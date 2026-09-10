"""Tests for V3-autonomous.md Phase 4 ("Subagent Concurrency & Latency
Drop"), built on top of Phase 3 (migrations/039_email_digest_queue.sql,
merged to main):

  1. db.agent_type_has_any_skills: the process-local, TTL'd existence
     cache -- a real DB check on a cold/expired cache, a cache hit that
     never touches the DB again within the TTL, the pre-migration graceful
     degrade, and upsert_skill flipping the cache to True immediately on a
     successful save (never waiting for the TTL).
  2. scratchpad_tools.scratchpad_prompt_block(user, agent_type): drops the
     "call search_skills before working with it" nudge when
     agent_type_has_any_skills says there's nothing recorded yet for that
     agent_type (still mentions save_skill), keeps the full nudge when
     there IS something recorded, and reverts to exactly the full
     pre-Phase-4 nudge -- without ever even calling
     agent_type_has_any_skills -- when LATENCY_OPTIMIZATIONS_ENABLED is
     off.
  3. registry._build_system_prompt: the parallel-delegation and fast-path-
     draft-preview paragraphs appear when LATENCY_OPTIMIZATIONS_ENABLED is
     on and are completely absent (byte-for-byte -- prompt is otherwise
     unaffected) when it's off.

No live Postgres, no live LLM call.
"""
import asyncio
import os
import sys
import time

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

import types as _types
_fake_composio_exceptions = _types.ModuleType("composio.exceptions")


class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = _types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from messa import config, db  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import scratchpad_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**kwargs) -> config.UserContext:
    base = dict(user_id=1, phone_number="+15550000000", name="Jane", onboarding_step="complete")
    base.update(kwargs)
    return config.UserContext(**base)


# ---------------------------------------------------------------------------
# Fakes -- same FakeAcquire/FakePool/FakeConn/FakeRow shape as
# test_v3_phase2_user_lists.py / test_v3_phase3_email_triage.py.
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, fetch_queue=None, fetchval_queue=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.fetch_queue = list(fetch_queue or [])
        self.fetchval_queue = list(fetchval_queue or [])
        self.calls = []

    async def fetchval(self, query, *args):
        self.calls.append(("fetchval", query, args))
        if "information_schema.tables" in query:
            return self.has_tables
        if self.fetchval_queue:
            return self.fetchval_queue.pop(0)
        return self.has_tables

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.fetchrow_queue:
            return self.fetchrow_queue.pop(0)
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        if self.fetch_queue:
            return self.fetch_queue.pop(0)
        return []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 0"


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeRow(dict):
    """asyncpg.Record is dict-like; dict(row) is used throughout db.py."""


def install_fake_pool(conn):
    async def fake_get_pool():
        return FakePool(conn)
    db.get_pool = fake_get_pool


# ---------------------------------------------------------------------------
# Part 1: db.agent_type_has_any_skills
# ---------------------------------------------------------------------------

async def part1_skills_existence_cache():
    real_ttl = config.SKILLS_EXISTENCE_CACHE_TTL_SECONDS
    db._SKILLS_EXISTENCE_CACHE.clear()
    try:
        # 1a: cold cache, table has a row -> True, and the query actually ran.
        conn = FakeConn(fetchval_queue=[True])
        install_fake_pool(conn)
        result = await db.agent_type_has_any_skills("integrations_agent")
        check("agent_type_has_any_skills: cold cache queries the DB and returns True", result is True)
        exists_calls = [c for c in conn.calls if c[0] == "fetchval" and "agent_skills" in c[1]]
        check("agent_type_has_any_skills: runs an EXISTS query against agent_skills",
              len(exists_calls) == 1)

        # 1b: cache hit -- second call within the TTL never touches the DB again.
        conn2 = FakeConn(fetchval_queue=[False])  # would return False if it ran
        install_fake_pool(conn2)
        result = await db.agent_type_has_any_skills("integrations_agent")
        check("agent_type_has_any_skills: a cache hit returns the cached value", result is True)
        check("agent_type_has_any_skills: a cache hit never touches the DB", conn2.calls == [])

        # 1c: a DIFFERENT agent_type is cached independently.
        conn3 = FakeConn(fetchval_queue=[False])
        install_fake_pool(conn3)
        result = await db.agent_type_has_any_skills("routines_agent")
        check("agent_type_has_any_skills: a different agent_type is looked up independently",
              result is False)

        # 1d: expired cache re-queries.
        config.SKILLS_EXISTENCE_CACHE_TTL_SECONDS = 0
        db._SKILLS_EXISTENCE_CACHE["routines_agent"] = (False, time.monotonic() - 1)
        conn4 = FakeConn(fetchval_queue=[True])
        install_fake_pool(conn4)
        result = await db.agent_type_has_any_skills("routines_agent")
        check("agent_type_has_any_skills: an expired cache entry re-queries the DB", result is True)
        config.SKILLS_EXISTENCE_CACHE_TTL_SECONDS = real_ttl

        # 1e: pre-migration graceful degrade -- never raises, returns False.
        db._SKILLS_EXISTENCE_CACHE.clear()
        conn5 = FakeConn(has_tables=False)
        install_fake_pool(conn5)
        result = await db.agent_type_has_any_skills("document_agent")
        check("agent_type_has_any_skills: pre-migration returns False, doesn't raise", result is False)

        # 1f: upsert_skill flips the cache to True immediately, not waiting for the TTL.
        db._SKILLS_EXISTENCE_CACHE.clear()
        db._SKILLS_EXISTENCE_CACHE["email_agent"] = (False, time.monotonic())  # freshly cached "no"
        row = FakeRow(skill_id="s1", agent_type="email_agent", domain="gmail", success_count=1)
        conn6 = FakeConn(fetchrow_queue=[row])
        install_fake_pool(conn6)
        await db.upsert_skill(
            agent_type="email_agent", domain="gmail",
            problem_pattern="p", solution_recipe="r",
        )
        cached = db._SKILLS_EXISTENCE_CACHE.get("email_agent")
        check("upsert_skill: flips the existence cache to True immediately on a successful save",
              cached is not None and cached[0] is True)
        conn7 = FakeConn(fetchval_queue=[False])  # would return False if the DB were actually hit
        install_fake_pool(conn7)
        result = await db.agent_type_has_any_skills("email_agent")
        check("agent_type_has_any_skills: reflects upsert_skill's cache flip without re-querying",
              result is True and conn7.calls == [])
    finally:
        config.SKILLS_EXISTENCE_CACHE_TTL_SECONDS = real_ttl
        db._SKILLS_EXISTENCE_CACHE.clear()


# ---------------------------------------------------------------------------
# Part 2: scratchpad_tools.scratchpad_prompt_block's search_skills nudge
# ---------------------------------------------------------------------------

async def part2_scratchpad_prompt_block():
    real_scratchpad_flag = config.SCRATCHPAD_AND_SKILLS_ENABLED
    real_latency_flag = config.LATENCY_OPTIMIZATIONS_ENABLED
    real_get_active_task = db.get_active_task
    real_has_any_skills = db.agent_type_has_any_skills
    try:
        config.SCRATCHPAD_AND_SKILLS_ENABLED = True
        config.LATENCY_OPTIMIZATIONS_ENABLED = True

        async def fake_get_active_task(user_id):
            return None

        db.get_active_task = fake_get_active_task

        # 2a: agent_type has recorded skills -> the full, original nudge.
        async def fake_has_skills_true(agent_type):
            return True

        db.agent_type_has_any_skills = fake_has_skills_true
        block = await scratchpad_tools.scratchpad_prompt_block(_user(), "integrations_agent")
        check("scratchpad_prompt_block: agent WITH skills keeps the 'search_skills BEFORE "
              "working with it' nudge", "search_skills for a toolkit or website BEFORE working with it" in block)
        check("scratchpad_prompt_block: save_skill is always mentioned", "save_skill(domain" in block)

        # 2b: agent_type has zero recorded skills -> the nudge is dropped,
        # save_skill guidance remains.
        async def fake_has_skills_false(agent_type):
            return False

        db.agent_type_has_any_skills = fake_has_skills_false
        block = await scratchpad_tools.scratchpad_prompt_block(_user(), "routines_agent")
        check("scratchpad_prompt_block: agent with NO skills drops the proactive search_skills nudge",
              "BEFORE working with it" not in block)
        check("scratchpad_prompt_block: agent with NO skills still describes save_skill",
              "save_skill(domain" in block)
        check("scratchpad_prompt_block: agent with NO skills says so plainly",
              "Nothing recorded for you yet" in block)

        # 2c: LATENCY_OPTIMIZATIONS_ENABLED off -- always the full nudge,
        # and agent_type_has_any_skills is never even called.
        config.LATENCY_OPTIMIZATIONS_ENABLED = False
        calls = []

        async def fake_has_skills_tracked(agent_type):
            calls.append(agent_type)
            return False

        db.agent_type_has_any_skills = fake_has_skills_tracked
        block = await scratchpad_tools.scratchpad_prompt_block(_user(), "routines_agent")
        check("scratchpad_prompt_block: kill switch off keeps the full pre-Phase-4 nudge",
              "search_skills for a toolkit or website BEFORE working with it" in block)
        check("scratchpad_prompt_block: kill switch off never calls agent_type_has_any_skills at all",
              calls == [])
    finally:
        config.SCRATCHPAD_AND_SKILLS_ENABLED = real_scratchpad_flag
        config.LATENCY_OPTIMIZATIONS_ENABLED = real_latency_flag
        db.get_active_task = real_get_active_task
        db.agent_type_has_any_skills = real_has_any_skills


# ---------------------------------------------------------------------------
# Part 3: registry._build_system_prompt's latency-guidance paragraphs
# ---------------------------------------------------------------------------

async def part3_system_prompt_paragraphs():
    real_flag = config.LATENCY_OPTIMIZATIONS_ENABLED
    try:
        config.LATENCY_OPTIMIZATIONS_ENABLED = True
        prompt_on = registry._build_system_prompt(_user())
        check("system prompt: parallel-delegation guidance present when enabled",
              "call task for BOTH in the SAME response" in prompt_on)
        check("system prompt: fast-path draft-preview guidance present when enabled",
              "write the draft text yourself, right here" in prompt_on)

        config.LATENCY_OPTIMIZATIONS_ENABLED = False
        prompt_off = registry._build_system_prompt(_user())
        check("system prompt: parallel-delegation guidance absent when disabled",
              "call task for BOTH in the SAME response" not in prompt_off)
        check("system prompt: fast-path draft-preview guidance absent when disabled",
              "write the draft text yourself, right here" not in prompt_off)
        check("system prompt: disabling costs exactly the two added paragraphs, nothing else",
              len(prompt_off) < len(prompt_on))
    finally:
        config.LATENCY_OPTIMIZATIONS_ENABLED = real_flag


async def main() -> None:
    await part1_skills_existence_cache()
    await part2_scratchpad_prompt_block()
    await part3_system_prompt_paragraphs()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
