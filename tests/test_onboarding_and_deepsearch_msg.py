"""Regression tests for this phase's changes:
  1. db.py: ONBOARDING_STEPS (now just name -> complete -- city/email are no
     longer sequential steps, see db.py's own comment), _initial_onboarding_step,
     save_profile_field's step transitions, and the has_prior_deepsearch_session
     helper.
  2. cli.py: _onboarding_complete_messages (the deterministic reveal,
     including its own live-view link) and run_message's extended
     _on_ai_message (deepsearch sit-back reminder + one-time "not just
     browsing" tip -- the deepsearch live-view link itself is a separate,
     later-sent mechanism, not part of this ack text; see Part 5's own
     comment).

Run: python3 tests/test_onboarding_and_deepsearch_msg.py (from the repo root)
"""
import asyncio
import os
import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    # No real .env in this environment (e.g. a fresh cloud sandbox) --
    # fall back to harmless dummy credentials so imports succeed and
    # fake-pool/fake-model tests can run. A real .env (e.g. the actual
    # dev machine/server) always wins untouched, since
    # messa.config's own load_dotenv() never overrides an already-set var.
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import cli, config, db  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"[PASS] {name}")
    else:
        FAIL += 1
        print(f"[FAIL] {name} {detail}")


# ---------------------------------------------------------------------------
# Part 1: db.py -- ONBOARDING_STEPS / _initial_onboarding_step
# ---------------------------------------------------------------------------

def part1_step_order():
    # Post-redesign (plans/glowing-forging-pumpkin.md Part 1): name is the
    # ONLY real onboarding gate now -- city/email are no longer sequential
    # steps at all, just always-answerable "profile enrichment" fields (see
    # agents/registry.py's _profile_enrichment_str). See db.py's
    # ONBOARDING_STEPS comment for the full rationale.
    check(
        "ONBOARDING_STEPS is name -> complete (no more awaiting_location/awaiting_email steps)",
        db.ONBOARDING_STEPS == ["awaiting_name", "complete"],
        detail=str(db.ONBOARDING_STEPS),
    )
    check(
        "_initial_onboarding_step(None) -> awaiting_name",
        db._initial_onboarding_step(None) == "awaiting_name",
    )
    check(
        "_initial_onboarding_step('Jane') -> complete (name is the only gate)",
        db._initial_onboarding_step("Jane") == "complete",
    )


# ---------------------------------------------------------------------------
# Part 2: db.py -- save_profile_field step transitions, against a fake pool
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, user_row: dict):
        self.user_row = user_row
        self.executed = []

    async def fetchval(self, sql, *args):
        # _has_column checks (email column) and _has_table -- assume present.
        if "information_schema.columns" in sql:
            return True
        if "information_schema.tables" in sql:
            return True
        return None

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        stripped = sql.strip()
        # Specific branches FIRST -- e.g. "SET email_prompt_skipped" is a
        # substring superset of the generic "SET email" check below (and
        # binds only one arg, not two), so it must be matched before it.
        if stripped in (
            "UPDATE users SET city_prompt_skipped = TRUE WHERE id = $1",
            "UPDATE users SET email_prompt_skipped = TRUE WHERE id = $1",
        ):
            col = stripped.split("SET", 1)[1].split("=", 1)[0].strip()
            self.user_row[col] = True
        elif "SET messa_email_local_part" in sql:
            self.user_row["messa_email_local_part"] = args[1]
        elif "SET name" in sql:
            self.user_row["name"] = args[1]
        elif "SET city" in sql:
            self.user_row["city"] = args[1]
        elif "SET email" in sql:
            self.user_row["email"] = args[1]
        elif "SET onboarding_step" in sql:
            self.user_row["onboarding_step"] = args[1]

    async def fetchrow(self, sql, *args):
        return dict(self.user_row)


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


async def part2_save_profile_field():
    # Post-redesign: only 'name' still advances onboarding_step (straight
    # to 'complete' -- there's no intermediate step left). City/email write
    # their column unconditionally, any time, regardless of onboarding_step,
    # and never touch onboarding_step themselves. 'skip' on city/email sets
    # the matching *_prompt_skipped flag instead of writing a literal value.
    db._column_cache.clear()
    user_row = {
        "id": 1, "name": None, "city": None, "email": None,
        "onboarding_step": "awaiting_name", "messa_email_local_part": None,
    }
    conn = FakeConn(user_row)
    pool = FakePool(conn)

    async def fake_get_pool():
        return pool

    orig_get_pool = db.get_pool
    orig_resolve_timezone = db.timeutil.resolve_timezone
    db.get_pool = fake_get_pool

    async def fake_resolve_timezone(city):
        return None  # keep this test focused on step transitions, not timezone resolution

    db.timeutil.resolve_timezone = fake_resolve_timezone
    try:
        row = await db.save_profile_field(1, "name", "Jane")
        check("save name advances awaiting_name -> complete (name is the only gate)",
              row["onboarding_step"] == "complete")

        row = await db.save_profile_field(1, "city", "Austin, TX")
        check("city writes immediately and does not touch onboarding_step",
              row["city"] == "Austin, TX" and row["onboarding_step"] == "complete")

        row = await db.save_profile_field(1, "email", "skip")
        check("skipping email sets email_prompt_skipped and leaves onboarding_step untouched",
              row.get("email_prompt_skipped") is True and row["onboarding_step"] == "complete")
        check("skip does not write a literal 'skip' into users.email", user_row.get("email") is None)

        try:
            await db.save_profile_field(1, "connect_email", "yes")
            check("save_profile_field rejects the removed 'connect_email' field", False)
        except ValueError:
            check("save_profile_field rejects the removed 'connect_email' field", True)
    finally:
        db.get_pool = orig_get_pool
        db.timeutil.resolve_timezone = orig_resolve_timezone


# ---------------------------------------------------------------------------
# Part 3: db.has_prior_deepsearch_session
# ---------------------------------------------------------------------------

class FakeSessionsConn:
    def __init__(self, has_table: bool, exists: bool):
        self.has_table = has_table
        self.exists = exists

    async def fetchval(self, sql, *args):
        if "information_schema.tables" in sql:
            return self.has_table
        if "EXISTS" in sql and "deepsearch_sessions" in sql:
            return self.exists
        return None


async def part3_has_prior_deepsearch_session():
    db._column_cache.clear()
    orig_get_pool = db.get_pool

    async def make_fake_pool(has_table, exists):
        conn = FakeSessionsConn(has_table, exists)

        async def fake_get_pool():
            return FakePool(conn)

        return fake_get_pool

    db.get_pool = await make_fake_pool(has_table=False, exists=False)
    try:
        result = await db.has_prior_deepsearch_session(1)
        check("has_prior_deepsearch_session is False pre-migration (no table)", result is False)
    finally:
        db.get_pool = orig_get_pool

    db.get_pool = await make_fake_pool(has_table=True, exists=False)
    try:
        result = await db.has_prior_deepsearch_session(1)
        check("has_prior_deepsearch_session is False for a brand-new user", result is False)
    finally:
        db.get_pool = orig_get_pool

    db.get_pool = await make_fake_pool(has_table=True, exists=True)
    try:
        result = await db.has_prior_deepsearch_session(1)
        check("has_prior_deepsearch_session is True once a session exists", result is True)
    finally:
        db.get_pool = orig_get_pool


# ---------------------------------------------------------------------------
# Part 4: cli._onboarding_complete_messages
# ---------------------------------------------------------------------------

async def part4_onboarding_reveal():
    orig_get_user_by_id = db.get_user_by_id

    def make_user(onboarding_step="complete", **kw):
        return config.UserContext(
            user_id=1, phone_number="+15551234567",
            onboarding_step=onboarding_step, **kw,
        )

    # Already complete before this turn -- no DB call, no messages.
    calls = []

    async def fake_get_user_by_id(uid):
        calls.append(uid)
        return {"onboarding_step": "complete", "name": "Jane"}

    db.get_user_by_id = fake_get_user_by_id
    try:
        user = make_user(onboarding_step="complete")
        msgs = await cli._onboarding_complete_messages(user)
        check("no reveal when onboarding was already complete this turn", msgs == [])
        check("no DB call when onboarding was already complete (short-circuited)", calls == [])

        # Mid-onboarding, and this turn did NOT complete it. (Post-redesign,
        # 'awaiting_name' is the only non-complete step left -- there's no
        # more "awaiting_email" to be pending on.)
        async def fake_still_pending(uid):
            return {"onboarding_step": "awaiting_name", "name": None}

        db.get_user_by_id = fake_still_pending
        user = make_user(onboarding_step="awaiting_name")
        msgs = await cli._onboarding_complete_messages(user)
        check("no reveal when onboarding step hasn't reached complete yet", msgs == [])

        # Transition detected: onboarding just completed this turn.
        async def fake_just_completed(uid):
            return {"onboarding_step": "complete", "name": "Jane", "messa_email_local_part": "jane123"}

        db.get_user_by_id = fake_just_completed
        user = make_user(
            onboarding_step="awaiting_name",
            name=None,  # stale pre-turn value -- greeting should use the FRESH row's name
            messa_email_local_part="jane123",
        )
        config.LIVE_VIEW_BASE_URL = "https://example.com"
        user.live_view_token = "tok_abc"
        msgs = await cli._onboarding_complete_messages(user)
        check("reveal fires exactly 2 messages when messa_email + live view are both available", len(msgs) == 2)
        check("greeting uses the freshly-fetched name, not the stale pre-turn None", "Jane" in msgs[0])
        check("not your typical chatbot framing is present", "chatbot" in msgs[0].lower())
        check("messa email address is included verbatim", "jane123@" in msgs[1])
        check("live view link is included", "https://example.com/live/tok_abc" in msgs[1])
        check("gmail hint ('just say the word') is present", "just say the word" in msgs[1])

        # Graceful degrade: no messa_email, no live view -- still get msg 1.
        # (name=None here too, not just on the stale pre-turn UserContext --
        # a falsy name is what keeps this from trying to provision a fresh
        # messa_email_local_part via a real DB call, same as the fresh row
        # genuinely having neither yet.)
        async def fake_degrade(uid):
            return {"onboarding_step": "complete", "name": None, "messa_email_local_part": None}

        db.get_user_by_id = fake_degrade
        user2 = make_user(onboarding_step="awaiting_name", name=None, messa_email_local_part=None)
        user2.live_view_token = None
        msgs2 = await cli._onboarding_complete_messages(user2)
        check("graceful degrade: still sends message 1 with no messa_email/live_view", len(msgs2) == 1)
    finally:
        db.get_user_by_id = orig_get_user_by_id


# ---------------------------------------------------------------------------
# Part 5: run_message's deepsearch sit-back reminder + one-time tip
# ---------------------------------------------------------------------------

class FakeAIMessage:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.type = "ai"


class FakeAgent:
    """Fakes agent.astream to fire a single AI message that delegates to
    deepsearch, then (for the compound-turn test) a second one."""

    def __init__(self, chunks):
        self.chunks = chunks

    async def astream(self, *a, **kw):
        for c in self.chunks:
            yield c


def _deepsearch_delegation_chunk(text: str):
    msg = FakeAIMessage(
        content=text,
        tool_calls=[{"name": "task", "args": {"subagent_type": "deepsearch", "description": "x"}}],
    )
    return {"messa": {"messages": [msg]}}


async def part5_deepsearch_reminder():
    orig_get_recent = db.get_recent_messages
    orig_append = db.append_message
    orig_get_user_by_id = db.get_user_by_id
    orig_has_prior = db.has_prior_deepsearch_session

    async def fake_get_recent(uid, limit=12):
        return []

    async def fake_append(uid, role, content, channel="cli"):
        return 1

    async def fake_get_user_by_id(uid):
        return {"onboarding_step": "complete", "name": "Jane"}  # onboarding_complete already true on user -> no reveal noise

    db.get_recent_messages = fake_get_recent
    db.append_message = fake_append
    db.get_user_by_id = fake_get_user_by_id

    try:
        # --- First-ever deepsearch: sit-back reminder AND first-time tip. ---
        async def fake_has_prior_false(uid):
            return False

        db.has_prior_deepsearch_session = fake_has_prior_false

        user = config.UserContext(
            user_id=1, phone_number="+15551234567", onboarding_step="complete",
        )
        config.LIVE_VIEW_BASE_URL = "https://example.com"
        user.live_view_token = "tok_xyz"

        agent = FakeAgent([_deepsearch_delegation_chunk("Checking flights now...")])
        sent = []

        async def fake_send(text):
            sent.append(text)

        await cli.run_message(user, agent, "find me a flight", send=fake_send)
        check("first-ever deepsearch: exactly one text sent for the single delegation", len(sent) == 1)
        check("sit-back reminder is present", "step away" in sent[0] and "text you" in sent[0])
        check("first-time 'not just browsing' tip is present", "not just browsing" in sent[0])
        # The live-view link itself is intentionally NOT part of this
        # acknowledgment text (see _on_ai_message's own comment in cli.py):
        # it's sent separately, straight from tools/deepsearch_tools.py's
        # _ensure_live_session, once a Browserbase session is genuinely
        # live -- not exercised by this fake-agent setup at all, so it's
        # correctly absent here rather than present.
        check("the live-view link is NOT inlined into the ack (sent separately once live)",
              "https://example.com/live/tok_xyz" not in sent[0])

        # --- Returning user (has a prior session): reminder, but NO tip. ---
        async def fake_has_prior_true(uid):
            return True

        db.has_prior_deepsearch_session = fake_has_prior_true
        agent2 = FakeAgent([_deepsearch_delegation_chunk("Looking into that...")])
        sent2 = []
        await cli.run_message(user, agent2, "find me another flight", send=lambda t: sent2.append(t) or asyncio.sleep(0))
        check("returning user: sit-back reminder still present", "step away" in sent2[0])
        check("returning user: no repeated 'not just browsing' tip", "not just browsing" not in sent2[0])

        # --- Compound turn: two deepsearch delegations in one exchange --
        # extras (reminder + tip) must appear exactly once, not twice.
        db.has_prior_deepsearch_session = fake_has_prior_false
        agent3 = FakeAgent([
            _deepsearch_delegation_chunk("Checking site A..."),
            _deepsearch_delegation_chunk("Checking site B..."),
        ])
        sent3 = []

        async def fake_send3(text):
            sent3.append(text)

        await cli.run_message(user, agent3, "compare two sites", send=fake_send3)
        combined = " ".join(sent3)
        check(
            "compound turn: sit-back reminder appears exactly once",
            combined.count("step away") == 1,
        )
    finally:
        db.get_recent_messages = orig_get_recent
        db.append_message = orig_append
        db.get_user_by_id = orig_get_user_by_id
        db.has_prior_deepsearch_session = orig_has_prior


async def main():
    part1_step_order()
    await part2_save_profile_field()
    await part3_has_prior_deepsearch_session()
    await part4_onboarding_reveal()
    await part5_deepsearch_reminder()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
