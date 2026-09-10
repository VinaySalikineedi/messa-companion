"""Tests for V3-autonomous.md Phase 6 ("Autonomous Project Capsules &
Multi-Modal Vault"), built as a THIN WRAPPER around the existing
cron_jobs/routines_agent autonomous-routine engine (migrations/
024_task_routines.sql) rather than a parallel scheduler -- see
migrations/041_project_capsules.sql's own header comment and
db._insert_project_capsule's docstring for the full "why."

Covers:
  1. db._insert_project_capsule: reuses _insert_cron_job verbatim on the
     same connection, inserts the wrapping project_capsules row, copies up
     any conflict-auto-supersede result, and degrades to plain
     _insert_cron_job behavior pre-migration-041 (no project_capsules
     table).
  2. db.set_cron_job_status's project-capsule sync: 'active'/'paused' map
     1:1, 'cancelled' maps to 'completed' (ended_reason=completed_by_agent),
     'expired' (ended_reason=expired), or plain 'cancelled' (anything
     else) -- and is a complete no-op pre-migration-041 or when the cron
     job has no linked capsule.
  3. db.py's other project-capsule functions: list/get/add asset/list
     assets/log event/list events/finish_project_capsule round trips, and
     their pre-migration graceful degrades.
  4. routines_tools._build_schedule_and_meta (shared by propose_create_
     routine and propose_create_project_capsule): exactly-one-of-schedule
     validation, bad-cron rejection, and -- the actual behavior change
     this refactor must NOT introduce -- propose_create_routine's own
     existing expiry semantics (including the expire_in_hours=0 "run
     forever" opt-out for autonomous routines) are unchanged.
  5. routines_tools.propose_create_project_capsule: stages a 'create_project'
     gated action with the right payload; rejects an empty title/goal;
     rejects expire_in_hours=0 (no "run forever" option for a project,
     unlike an ordinary routine); uses config.PROJECT_DEFAULT_EXPIRE_HOURS/
     PROJECT_MAX_EXPIRE_HOURS, not the ordinary routine bounds.
  6. list_my_project_capsules / get_project_capsule_details formatting;
     pause/resume/cancel_project_capsule delegating to db.set_cron_job_status
     with the right status/meta; finish_project_capsule and
     add_project_capsule_asset/log_project_capsule_event tool wiring.
  7. config.PROJECT_CAPSULES_ENABLED kill switch: build_routines_tools
     omits every project tool when off; build_routines_subagent's prompt
     omits the project-capsules paragraph when off, includes it when on.
  8. registry.py: confirm_pending_action's create_routine/create_project
     shared "superseded job" branch; _active_project_capsules_paragraph's
     empty-when-none and populated-listing behavior.

No live Postgres, no live LLM call.
"""
import asyncio
import json
import os
import sys

from datetime import datetime, timedelta, timezone
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

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from messa import config, db  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import routines_tools  # noqa: E402
import messa.server as server  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


USER = config.UserContext(
    user_id=1, phone_number="+15551234567", name="Test User",
    timezone="America/New_York", onboarding_step="complete",
)


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
        if "information_schema" in query:
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

    def transaction(self):
        return _FakeTxn()


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


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
# Part 1: db._insert_project_capsule
# ---------------------------------------------------------------------------

async def part1_insert_project_capsule():
    payload = {
        "title": "Delta $850 refund dispute",
        "prompt_or_task": "Follow up on the $850 Delta refund until it's resolved.",
        "cron_expression": "0 9 * * *",
        "user_timezone": "America/New_York",
        "next_run_at": datetime.now(timezone.utc) + timedelta(days=1),
        "execution_mode": "autonomous",
        "meta": {"expire_at": "2026-12-01T00:00:00+00:00"},
    }

    # 1a: success -- reuses _insert_cron_job, then wraps it in a
    # project_capsules row, nesting the cron result under "cron_job".
    cron_row = FakeRow(id=501, prompt_or_task=payload["prompt_or_task"], status="active")
    project_row = FakeRow(
        id=9001, user_id=1, cron_job_id=501, title=payload["title"],
        goal=payload["prompt_or_task"], status="active", outcome_summary=None,
    )
    conn = FakeConn(has_tables=True, fetchrow_queue=[cron_row, project_row])
    result = await db._insert_project_capsule(conn, 1, payload)
    check("_insert_project_capsule: returns the new capsule row", result["id"] == 9001)
    check("_insert_project_capsule: nests the underlying cron job's own result",
          result["cron_job"]["id"] == 501)
    check("_insert_project_capsule: no _superseded_job key when nothing was superseded",
          "_superseded_job" not in result)
    insert_project_call = next(c for c in conn.calls if c[0] == "fetchrow" and "INSERT INTO project_capsules" in c[1])
    check("_insert_project_capsule: inserts with cron_job_id/title/goal from the payload",
          insert_project_call[2] == (1, 501, payload["title"], payload["prompt_or_task"]))

    # 1b: conflict-auto-supersede result is copied up to this function's own
    # top level (same key registry.py's confirm_pending_action already
    # reads for a plain create_routine).
    real_flag = config.CONFLICT_AUTO_SUPERSEDE_ENABLED
    try:
        config.CONFLICT_AUTO_SUPERSEDE_ENABLED = True
        payload_with_email = dict(payload, prompt_or_task="Email kj@mangustacap.com about the refund status.")
        cron_row2 = FakeRow(id=502, prompt_or_task=payload_with_email["prompt_or_task"], status="active")
        old_conflicting = FakeRow(id=77, prompt_or_task="Email kj@mangustacap.com weekly")
        cancelled_row = FakeRow(id=77, prompt_or_task="Email kj@mangustacap.com weekly", status="cancelled")
        project_row2 = FakeRow(
            id=9002, user_id=1, cron_job_id=502, title=payload["title"],
            goal=payload_with_email["prompt_or_task"], status="active",
        )
        conn2 = FakeConn(
            has_tables=True,
            # cron_row2 (the new job's own INSERT), cancelled_row (the
            # superseded job's UPDATE), None (that superseded job -- an
            # ordinary routine -- has no linked project_capsules row, so
            # _sync_linked_project_capsule_status's own UPDATE...RETURNING
            # finds nothing), project_row2 (this capsule's own INSERT).
            fetchrow_queue=[cron_row2, cancelled_row, None, project_row2],
            fetch_queue=[[old_conflicting]],
        )
        result2 = await db._insert_project_capsule(conn2, 1, payload_with_email)
        check("_insert_project_capsule: copies up _superseded_job when the underlying cron insert superseded one",
              result2.get("_superseded_job", {}).get("id") == 77)
        sync_attempt = next(
            (c for c in conn2.calls if c[0] == "fetchrow" and "UPDATE project_capsules" in c[1]), None
        )
        check("_insert_project_capsule: the superseded job's own capsule-sync ran (found no linked capsule, safely)",
              sync_attempt is not None)
    finally:
        config.CONFLICT_AUTO_SUPERSEDE_ENABLED = real_flag

    # 1c: pre-migration-041 (no project_capsules table) degrades to
    # _insert_cron_job's own plain result -- no capsule row attempted.
    cron_row3 = FakeRow(id=503, prompt_or_task=payload["prompt_or_task"], status="active")
    conn3 = FakeConn(has_tables=False, fetchrow_queue=[cron_row3])
    result3 = await db._insert_project_capsule(conn3, 1, payload)
    check("_insert_project_capsule: pre-migration degrades to the plain cron job row",
          result3 == dict(cron_row3))
    check("_insert_project_capsule: pre-migration never attempts a project_capsules INSERT",
          not any("project_capsules" in c[1] for c in conn3.calls if c[0] == "fetchrow"))


# ---------------------------------------------------------------------------
# Part 2: db.set_cron_job_status's project-capsule sync
# ---------------------------------------------------------------------------

async def part2_set_cron_job_status_sync():
    # 2a: 'active' maps straight across, no terminal-state timeline event.
    updated = FakeRow(id=501, status="active", meta=json.dumps({}))
    capsule_row = FakeRow(id=9)
    conn = FakeConn(has_tables=True, fetchrow_queue=[updated, capsule_row])
    install_fake_pool(conn)
    await db.set_cron_job_status(1, 501, "active")
    sync_call = next(c for c in conn.calls if c[0] == "fetchrow" and "UPDATE project_capsules" in c[1])
    check("set_cron_job_status: 'active' syncs the linked capsule to 'active'",
          sync_call[2] == (501, "active", None))
    check("set_cron_job_status: 'active' logs no timeline event (not a terminal state)",
          not any("project_timeline_events" in c[1] for c in conn.calls if c[0] == "execute"))

    # 2b: 'paused' maps straight across, no terminal-state timeline event.
    updated = FakeRow(id=501, status="paused", meta=json.dumps({}))
    capsule_row = FakeRow(id=9)
    conn = FakeConn(has_tables=True, fetchrow_queue=[updated, capsule_row])
    install_fake_pool(conn)
    await db.set_cron_job_status(1, 501, "paused")
    sync_call = next(c for c in conn.calls if c[0] == "fetchrow" and "UPDATE project_capsules" in c[1])
    check("set_cron_job_status: 'paused' syncs the linked capsule to 'paused'",
          sync_call[2] == (501, "paused", None))
    check("set_cron_job_status: 'paused' logs no timeline event", not any(
        "project_timeline_events" in c[1] for c in conn.calls if c[0] == "execute"))

    # 2c: 'cancelled' with ended_reason='completed_by_agent' -> 'completed',
    # copies the outcome onto the capsule, and logs a completion event --
    # the actual fix for Issue 3 (QA report): this now happens even when
    # the model called the ORDINARY finish_routine, not the project-
    # specific finish_project_capsule tool.
    existing = FakeRow(meta=json.dumps({}))
    updated = FakeRow(id=501, status="cancelled", meta=json.dumps({"ended_reason": "completed_by_agent", "outcome": "Refund received."}))
    capsule_row = FakeRow(id=9)
    conn = FakeConn(has_tables=True, fetchrow_queue=[existing, updated, capsule_row])
    install_fake_pool(conn)
    await db.set_cron_job_status(1, 501, "cancelled", {"ended_reason": "completed_by_agent", "outcome": "Refund received."})
    sync_call = next(c for c in conn.calls if c[0] == "fetchrow" and "UPDATE project_capsules" in c[1])
    check("set_cron_job_status: 'cancelled'+completed_by_agent syncs the capsule to 'completed' with its outcome",
          sync_call[2] == (501, "completed", "Refund received."))
    event_call = next(c for c in conn.calls if c[0] == "execute" and "project_timeline_events" in c[1])
    check("set_cron_job_status: logs a completion timeline event with the outcome text",
          event_call[2] == (9, "Completed: Refund received."))

    # 2d: 'cancelled' with ended_reason='expired' -> 'expired', logs an
    # expiry timeline event.
    existing = FakeRow(meta=json.dumps({}))
    updated = FakeRow(id=501, status="cancelled", meta=json.dumps({"ended_reason": "expired"}))
    capsule_row = FakeRow(id=9)
    conn = FakeConn(has_tables=True, fetchrow_queue=[existing, updated, capsule_row])
    install_fake_pool(conn)
    await db.set_cron_job_status(1, 501, "cancelled", {"ended_reason": "expired"})
    sync_call = next(c for c in conn.calls if c[0] == "fetchrow" and "UPDATE project_capsules" in c[1])
    check("set_cron_job_status: 'cancelled'+expired syncs the capsule to 'expired'",
          sync_call[2] == (501, "expired", None))
    event_call = next(c for c in conn.calls if c[0] == "execute" and "project_timeline_events" in c[1])
    check("set_cron_job_status: logs an expiry timeline event", "Expired" in event_call[2][1])

    # 2e: 'cancelled' with any other (or no) reason -> plain 'cancelled',
    # logs a cancellation timeline event.
    existing = FakeRow(meta=json.dumps({}))
    updated = FakeRow(id=501, status="cancelled", meta=json.dumps({"ended_reason": "cancelled_by_user"}))
    capsule_row = FakeRow(id=9)
    conn = FakeConn(has_tables=True, fetchrow_queue=[existing, updated, capsule_row])
    install_fake_pool(conn)
    await db.set_cron_job_status(1, 501, "cancelled", {"ended_reason": "cancelled_by_user"})
    sync_call = next(c for c in conn.calls if c[0] == "fetchrow" and "UPDATE project_capsules" in c[1])
    check("set_cron_job_status: 'cancelled'+cancelled_by_user syncs the capsule to 'cancelled'",
          sync_call[2] == (501, "cancelled", None))
    event_call = next(c for c in conn.calls if c[0] == "execute" and "project_timeline_events" in c[1])
    check("set_cron_job_status: logs a cancellation timeline event", "Cancelled" in event_call[2][1])

    # 2e-2: 'cancelled' with ended_reason='superseded' (from conflict
    # auto-supersede) -> plain 'cancelled', with a reason-specific event.
    existing = FakeRow(meta=json.dumps({}))
    updated = FakeRow(id=501, status="cancelled", meta=json.dumps({"ended_reason": "superseded"}))
    capsule_row = FakeRow(id=9)
    conn = FakeConn(has_tables=True, fetchrow_queue=[existing, updated, capsule_row])
    install_fake_pool(conn)
    await db.set_cron_job_status(1, 501, "cancelled", {"ended_reason": "superseded"})
    event_call = next(c for c in conn.calls if c[0] == "execute" and "project_timeline_events" in c[1])
    check("set_cron_job_status: a superseded cancel logs a reason-specific timeline event",
          "superseded" in event_call[2][1])

    # 2f: pre-migration-041 (no project_capsules table) -- no sync attempted,
    # still returns the updated cron row.
    updated = FakeRow(id=501, status="active", meta=json.dumps({}))
    conn = FakeConn(has_tables=False, fetchrow_queue=[updated])
    install_fake_pool(conn)
    result = await db.set_cron_job_status(1, 501, "active")
    check("set_cron_job_status: pre-migration still returns the updated cron row",
          result["id"] == 501)
    check("set_cron_job_status: pre-migration never touches project_capsules",
          not any("project_capsules" in c[1] for c in conn.calls if c[0] == "fetchrow"))

    # 2g: no matching cron job (fetchrow returns None) -- returns {} without
    # attempting any capsule sync.
    conn = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn)
    result = await db.set_cron_job_status(1, 999, "active")
    check("set_cron_job_status: no such cron job returns {}", result == {})
    check("set_cron_job_status: no such cron job never touches project_capsules",
          not any("project_capsules" in c[1] for c in conn.calls if c[0] == "fetchrow"))

    # 2h: cron job has NO linked capsule (the UPDATE...WHERE cron_job_id=$1
    # matches nothing) -- no timeline event, no crash.
    updated = FakeRow(id=501, status="active", meta=json.dumps({}))
    conn = FakeConn(has_tables=True, fetchrow_queue=[updated, None])
    install_fake_pool(conn)
    await db.set_cron_job_status(1, 501, "active")
    check("set_cron_job_status: an ordinary routine (no linked capsule) logs no timeline event",
          not any("project_timeline_events" in c[1] for c in conn.calls if c[0] == "execute"))


# ---------------------------------------------------------------------------
# Part 3: db.py's other project-capsule functions
# ---------------------------------------------------------------------------

async def part3_other_db_functions():
    # list_project_capsules
    rows = [FakeRow(id=1, title="A"), FakeRow(id=2, title="B")]
    conn = FakeConn(has_tables=True, fetch_queue=[rows])
    install_fake_pool(conn)
    result = await db.list_project_capsules(1)
    check("list_project_capsules: returns every row", result == rows)
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.list_project_capsules(1)
    check("list_project_capsules: pre-migration returns []", result == [])

    # get_project_capsule
    row = FakeRow(id=9, title="A")
    conn = FakeConn(has_tables=True, fetchrow_queue=[row])
    install_fake_pool(conn)
    result = await db.get_project_capsule(1, 9)
    check("get_project_capsule: returns the found row", result == dict(row))
    conn = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn)
    result = await db.get_project_capsule(1, 999)
    check("get_project_capsule: returns None when not found", result is None)
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.get_project_capsule(1, 9)
    check("get_project_capsule: pre-migration returns None", result is None)

    # add_project_capsule_asset / list_project_capsule_assets
    asset_row = FakeRow(id=1, project_id=9, source_url="https://cdn/x.jpg", description="receipt")
    conn = FakeConn(has_tables=True, fetchrow_queue=[asset_row])
    install_fake_pool(conn)
    result = await db.add_project_capsule_asset(9, "https://cdn/x.jpg", "receipt")
    check("add_project_capsule_asset: returns the inserted row", result == dict(asset_row))
    insert_call = next(c for c in conn.calls if c[0] == "fetchrow")
    check("add_project_capsule_asset: inserts with project_id/source_url/description",
          insert_call[2] == (9, "https://cdn/x.jpg", "receipt"))

    conn = FakeConn(has_tables=True, fetch_queue=[[asset_row]])
    install_fake_pool(conn)
    result = await db.list_project_capsule_assets(9)
    check("list_project_capsule_assets: returns every asset", result == [asset_row])

    # log_project_capsule_event / list_project_capsule_events
    conn = FakeConn(has_tables=True)
    install_fake_pool(conn)
    await db.log_project_capsule_event(9, "Emailed Delta support.")
    insert_call = next(c for c in conn.calls if c[0] == "execute")
    check("log_project_capsule_event: inserts with the right args",
          insert_call[2] == (9, "Emailed Delta support."))

    event_row = FakeRow(id=1, project_id=9, event_text="Emailed Delta support.")
    conn = FakeConn(has_tables=True, fetch_queue=[[event_row]])
    install_fake_pool(conn)
    result = await db.list_project_capsule_events(9)
    check("list_project_capsule_events: returns every event", result == [event_row])

    # --- P6 report Issue 1 fix: these five functions used to run raw SQL
    # with NO _has_table guard at all -- a crash (asyncpg.UndefinedTableError)
    # pre-migration-041, not a graceful degrade. Each must now safely
    # return {}/[]/None and touch NOTHING else pre-migration.
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.add_project_capsule_asset(9, "https://cdn/x.jpg", "receipt")
    check("Issue 1 fix: add_project_capsule_asset degrades to {} pre-migration, doesn't crash", result == {})
    check("Issue 1 fix: add_project_capsule_asset never attempts the INSERT pre-migration",
          not any(c[0] == "fetchrow" for c in conn.calls))

    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.list_project_capsule_assets(9)
    check("Issue 1 fix: list_project_capsule_assets degrades to [] pre-migration, doesn't crash", result == [])

    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    await db.log_project_capsule_event(9, "x")  # must not raise
    check("Issue 1 fix: log_project_capsule_event never attempts the INSERT pre-migration",
          not any(c[0] == "execute" for c in conn.calls))

    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.list_project_capsule_events(9)
    check("Issue 1 fix: list_project_capsule_events degrades to [] pre-migration, doesn't crash", result == [])

    # get_project_capsule_by_cron_job (new -- feeds server.py's cadence-loop
    # project-context fix, Issue 3)
    capsule_row = FakeRow(id=9, cron_job_id=501, title="Delta refund", status="active")
    conn = FakeConn(has_tables=True, fetchrow_queue=[capsule_row])
    install_fake_pool(conn)
    result = await db.get_project_capsule_by_cron_job(501)
    check("get_project_capsule_by_cron_job: finds the capsule linked to this cron job", result == dict(capsule_row))
    conn = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn)
    result = await db.get_project_capsule_by_cron_job(999)
    check("get_project_capsule_by_cron_job: None for an ordinary routine with no linked capsule", result is None)
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.get_project_capsule_by_cron_job(501)
    check("get_project_capsule_by_cron_job: pre-migration returns None, doesn't crash", result is None)

    # finish_project_capsule
    project_row = FakeRow(id=9, user_id=1, cron_job_id=501, title="A", goal="do it", status="active")
    updated_capsule = FakeRow(id=9, status="completed", outcome_summary="Refund received.")
    conn = FakeConn(has_tables=True, fetchrow_queue=[project_row, updated_capsule])
    install_fake_pool(conn)
    real_set_status = db.set_cron_job_status
    sync_calls = []

    async def fake_set_status(user_id, cron_id, status, meta_patch=None):
        sync_calls.append((user_id, cron_id, status, meta_patch))
        return {"id": cron_id}

    db.set_cron_job_status = fake_set_status
    try:
        result = await db.finish_project_capsule(1, 9, "Refund received.")
        check("finish_project_capsule: cancels the underlying cron job with completed_by_agent",
              sync_calls == [(1, 501, "cancelled", {"ended_reason": "completed_by_agent", "outcome": "Refund received."})])
        check("finish_project_capsule: records the outcome on the capsule row",
              result["outcome_summary"] == "Refund received.")
    finally:
        db.set_cron_job_status = real_set_status

    # finish_project_capsule: orphaned capsule (no cron_job_id) -- logs its
    # OWN completion timeline event, since set_cron_job_status's sync never
    # runs when there's no linked cron job.
    orphan_project_row = FakeRow(id=10, user_id=1, cron_job_id=None, title="B", goal="do it", status="active")
    updated_orphan_capsule = FakeRow(id=10, status="completed", outcome_summary="Done.")
    conn = FakeConn(has_tables=True, fetchrow_queue=[orphan_project_row, updated_orphan_capsule])
    install_fake_pool(conn)
    result = await db.finish_project_capsule(1, 10, "Done.")
    check("finish_project_capsule: orphaned capsule (no cron job) still completes",
          result["status"] == "completed")
    event_call = next((c for c in conn.calls if c[0] == "execute" and "project_timeline_events" in c[1]), None)
    check("finish_project_capsule: orphaned capsule logs its own completion timeline event",
          event_call is not None and event_call[2] == (10, "Completed: Done."))

    # finish_project_capsule: no such capsule for this user -> {}
    conn = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn)
    result = await db.finish_project_capsule(1, 999, "x")
    check("finish_project_capsule: returns {} for an unknown/foreign capsule", result == {})

    # finish_project_capsule: pre-migration -- {} without crashing (Issue 1 fix)
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.finish_project_capsule(1, 9, "x")
    check("Issue 1 fix: finish_project_capsule degrades to {} pre-migration, doesn't crash", result == {})

    # cancel_orphaned_project_capsule (new -- Issue 6 fix: cancelling an
    # orphaned capsule must set 'cancelled', never 'completed')
    cancelled_row = FakeRow(id=10, status="cancelled")
    conn = FakeConn(has_tables=True, fetchrow_queue=[cancelled_row])
    install_fake_pool(conn)
    result = await db.cancel_orphaned_project_capsule(1, 10)
    check("cancel_orphaned_project_capsule: sets status to 'cancelled', not 'completed'",
          result["status"] == "cancelled")
    update_call = next(c for c in conn.calls if c[0] == "fetchrow")
    check("cancel_orphaned_project_capsule: the UPDATE itself sets status = 'cancelled'",
          "status = 'cancelled'" in update_call[1])
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)
    result = await db.cancel_orphaned_project_capsule(1, 10)
    check("cancel_orphaned_project_capsule: pre-migration returns {}, doesn't crash", result == {})


# ---------------------------------------------------------------------------
# Part 4: _build_schedule_and_meta via propose_create_routine (must NOT
# regress this refactor's before-behavior)
# ---------------------------------------------------------------------------

async def part4_build_schedule_and_meta_via_routine():
    tools = routines_tools.build_routines_tools(USER)
    propose_routine = next(t for t in tools if t.name == "propose_create_routine")

    async def fake_propose_action(uid, action_type, payload):
        return {"id": 55, **payload}

    real_propose = db.propose_action
    db.propose_action = fake_propose_action
    try:
        reply = await propose_routine.coroutine(
            prompt_or_task="check my inbox", execution_mode="autonomous",
            cron_expression="not a cron",
        )
        check("propose_create_routine: still rejects an invalid cron expression", reply.startswith("ERROR"))

        reply = await propose_routine.coroutine(
            prompt_or_task="check my inbox", execution_mode="autonomous",
            cron_expression="0 8 * * *", run_once_in_minutes=5,
        )
        check("propose_create_routine: still rejects both schedule kinds given at once", reply.startswith("ERROR"))

        reply = await propose_routine.coroutine(
            prompt_or_task="check my inbox", execution_mode="notify",
        )
        check("propose_create_routine: still rejects neither schedule kind given", reply.startswith("ERROR"))

        reply = await propose_routine.coroutine(
            prompt_or_task="watch this forever", execution_mode="autonomous",
            cron_expression="0 8 * * *", expire_in_hours=0,
        )
        check("propose_create_routine: 'autonomous' + expire_in_hours=0 is still accepted (run-forever opt-out)",
              reply.startswith("Proposed"))
    finally:
        db.propose_action = real_propose


# ---------------------------------------------------------------------------
# Part 5: propose_create_project_capsule
# ---------------------------------------------------------------------------

async def part5_propose_create_project_capsule():
    tools = routines_tools.build_routines_tools(USER)
    propose_project = next(t for t in tools if t.name == "propose_create_project_capsule")

    captured = []

    async def fake_propose_action(uid, action_type, payload):
        captured.append((uid, action_type, payload))
        return {"id": 77}

    real_propose = db.propose_action
    db.propose_action = fake_propose_action
    try:
        reply = await propose_project.coroutine(title="  ", goal="do the thing", cadence_cron_expression="0 9 * * *")
        check("propose_create_project_capsule: rejects an empty title", reply.startswith("ERROR"))

        reply = await propose_project.coroutine(title="Refund", goal="   ", cadence_cron_expression="0 9 * * *")
        check("propose_create_project_capsule: rejects an empty goal", reply.startswith("ERROR"))

        reply = await propose_project.coroutine(
            title="Refund", goal="dispute the charge", cadence_cron_expression="not a cron",
        )
        check("propose_create_project_capsule: rejects an invalid cron expression", reply.startswith("ERROR"))

        reply = await propose_project.coroutine(
            title="Refund", goal="dispute the charge", cadence_cron_expression="0 9 * * *", expire_in_hours=0,
        )
        check("propose_create_project_capsule: rejects expire_in_hours=0 -- no 'run forever' option for a project",
              reply.startswith("ERROR") and "no 'run forever'" in reply)

        captured.clear()
        reply = await propose_project.coroutine(
            title="Delta refund", goal="dispute the $850 charge", cadence_cron_expression="0 9 * * *",
        )
        check("propose_create_project_capsule: proposes a 'create_project' gated action", captured and captured[0][1] == "create_project")
        payload = captured[0][2]
        check("propose_create_project_capsule: payload carries title and goal-as-prompt_or_task",
              payload["title"] == "Delta refund" and payload["prompt_or_task"] == "dispute the $850 charge")
        check("propose_create_project_capsule: execution_mode is always 'autonomous'",
              payload["execution_mode"] == "autonomous")
        check("propose_create_project_capsule: applies the PROJECT (not ROUTINE) default expiry bound",
              payload["meta"].get("expire_at") is not None)
        check("propose_create_project_capsule: reply names the pending id", "#77" in reply)

        # Default expiry uses config.PROJECT_DEFAULT_EXPIRE_HOURS, capped at
        # config.PROJECT_MAX_EXPIRE_HOURS -- not the ordinary routine's own
        # (much smaller) bounds.
        captured.clear()
        real_default, real_max = config.PROJECT_DEFAULT_EXPIRE_HOURS, config.PROJECT_MAX_EXPIRE_HOURS
        try:
            config.PROJECT_DEFAULT_EXPIRE_HOURS = 100.0
            config.PROJECT_MAX_EXPIRE_HOURS = 200.0
            await propose_project.coroutine(
                title="X", goal="Y", cadence_cron_expression="0 9 * * *", expire_in_hours=999,
            )
            payload = captured[0][2]
            expire_at = datetime.fromisoformat(payload["meta"]["expire_at"])
            now = datetime.now(expire_at.tzinfo)
            hours_out = (expire_at - now).total_seconds() / 3600
            check("propose_create_project_capsule: caps an over-max expire_in_hours at PROJECT_MAX_EXPIRE_HOURS",
                  190 <= hours_out <= 200)
        finally:
            config.PROJECT_DEFAULT_EXPIRE_HOURS, config.PROJECT_MAX_EXPIRE_HOURS = real_default, real_max

        # QA report Issue 4: an oversized title used to crash on the
        # DB's VARCHAR(255) column instead of failing cleanly with a
        # message the model can act on (shorten the title, keep detail in
        # `goal`). config.PROJECT_CAPSULE_TITLE_MAX_LENGTH keeps headroom
        # under that ceiling.
        captured.clear()
        too_long_title = "x" * (config.PROJECT_CAPSULE_TITLE_MAX_LENGTH + 1)
        reply = await propose_project.coroutine(
            title=too_long_title, goal="dispute the charge", cadence_cron_expression="0 9 * * *",
        )
        check("propose_create_project_capsule: rejects a too-long title before it ever reaches the DB",
              reply.startswith("ERROR") and not captured)
        check("propose_create_project_capsule: too-long-title error names the max length",
              str(config.PROJECT_CAPSULE_TITLE_MAX_LENGTH) in reply)

        captured.clear()
        exactly_max_title = "x" * config.PROJECT_CAPSULE_TITLE_MAX_LENGTH
        reply = await propose_project.coroutine(
            title=exactly_max_title, goal="dispute the charge", cadence_cron_expression="0 9 * * *",
        )
        check("propose_create_project_capsule: a title at exactly the max length is accepted",
              not reply.startswith("ERROR") and captured)
    finally:
        db.propose_action = real_propose


# ---------------------------------------------------------------------------
# Part 6: list/get/pause/resume/cancel/finish/asset/event tools
# ---------------------------------------------------------------------------

async def part6_project_tools():
    tools = routines_tools.build_routines_tools(USER)
    by_name = {t.name: t for t in tools}

    real_list = db.list_project_capsules
    real_get = db.get_project_capsule
    real_assets = db.list_project_capsule_assets
    real_events = db.list_project_capsule_events
    real_set_status = db.set_cron_job_status
    real_add_asset = db.add_project_capsule_asset
    real_log_event = db.log_project_capsule_event
    real_finish = db.finish_project_capsule

    try:
        # list_my_project_capsules
        async def fake_list(uid):
            return [{"id": 1, "title": "Refund", "status": "active", "next_run_at": "2026-09-11T09:00:00+00:00"}]

        db.list_project_capsules = fake_list
        reply = await by_name["list_my_project_capsules"].coroutine()
        check("list_my_project_capsules: lists id/title/status", "#1" in reply and "Refund" in reply and "active" in reply)

        async def fake_list_empty(uid):
            return []

        db.list_project_capsules = fake_list_empty
        reply = await by_name["list_my_project_capsules"].coroutine()
        check("list_my_project_capsules: says so when there are none", "No project capsules" in reply)

        # get_project_capsule_details
        async def fake_get(uid, pid):
            return {"id": 9, "title": "Refund", "status": "active", "goal": "dispute it", "outcome_summary": None}

        async def fake_assets(pid):
            return [{"description": "receipt photo", "source_url": "https://cdn/x.jpg"}]

        async def fake_events(pid, limit=20):
            return [{"created_at": "2026-09-10", "event_text": "Emailed support."}]

        db.get_project_capsule = fake_get
        db.list_project_capsule_assets = fake_assets
        db.list_project_capsule_events = fake_events
        reply = await by_name["get_project_capsule_details"].coroutine(project_id=9)
        check("get_project_capsule_details: shows the goal", "dispute it" in reply)
        check("get_project_capsule_details: shows vault assets", "receipt photo" in reply)
        check("get_project_capsule_details: shows recent timeline", "Emailed support." in reply)

        async def fake_get_none(uid, pid):
            return None

        db.get_project_capsule = fake_get_none
        reply = await by_name["get_project_capsule_details"].coroutine(project_id=999)
        check("get_project_capsule_details: not-found message for an unknown id", "No project capsule" in reply)

        # pause/resume/cancel_project_capsule -> db.set_cron_job_status
        async def fake_get_with_cron(uid, pid):
            return {"id": 9, "cron_job_id": 501, "status": "active"}

        db.get_project_capsule = fake_get_with_cron
        status_calls = []

        async def fake_set_status(uid, cron_id, status, meta_patch=None):
            status_calls.append((uid, cron_id, status, meta_patch))
            return {"id": cron_id}

        db.set_cron_job_status = fake_set_status

        status_calls.clear()
        await by_name["pause_project_capsule"].coroutine(project_id=9)
        check("pause_project_capsule: pauses the linked cron job", status_calls == [(1, 501, "paused", None)])

        status_calls.clear()
        await by_name["resume_project_capsule"].coroutine(project_id=9)
        check("resume_project_capsule: resumes the linked cron job", status_calls == [(1, 501, "active", None)])

        status_calls.clear()
        await by_name["cancel_project_capsule"].coroutine(project_id=9)
        check("cancel_project_capsule: cancels the linked cron job with cancelled_by_user",
              status_calls == [(1, 501, "cancelled", {"ended_reason": "cancelled_by_user"})])

        # cancel_project_capsule: orphaned path (QA report Issue 6) -- no
        # linked cron job to route a status change through, so the tool
        # must call db.cancel_orphaned_project_capsule (which marks
        # 'cancelled', never 'completed') and log its own timeline event
        # directly, instead of falling through to db.finish_project_capsule
        # (which always marks 'completed' -- would misrepresent a
        # given-up-on project as a successfully finished one).
        async def fake_get_orphaned(uid, pid):
            return {"id": 9, "cron_job_id": None, "status": "active"}

        orphan_cancel_calls = []

        async def fake_cancel_orphaned(uid, pid):
            orphan_cancel_calls.append((uid, pid))

        real_cancel_orphaned = db.cancel_orphaned_project_capsule
        db.get_project_capsule = fake_get_orphaned
        db.cancel_orphaned_project_capsule = fake_cancel_orphaned
        status_calls.clear()
        log_calls_orphan = []

        async def fake_log_orphan(pid, text):
            log_calls_orphan.append((pid, text))

        db.log_project_capsule_event = fake_log_orphan
        try:
            reply = await by_name["cancel_project_capsule"].coroutine(project_id=9)
            check("cancel_project_capsule: orphaned project calls cancel_orphaned_project_capsule, not set_cron_job_status",
                  orphan_cancel_calls == [(1, 9)] and not status_calls)
            check("cancel_project_capsule: orphaned project logs its own 'Cancelled by the user.' event",
                  log_calls_orphan == [(9, "Cancelled by the user.")])
            check("cancel_project_capsule: orphaned project still replies with confirmation", "#9" in reply)
        finally:
            db.cancel_orphaned_project_capsule = real_cancel_orphaned
            db.get_project_capsule = fake_get_with_cron

        # finish_project_capsule
        finish_calls = []

        async def fake_finish(uid, pid, outcome):
            finish_calls.append((uid, pid, outcome))
            return {"id": pid}

        log_calls = []

        async def fake_log(pid, text):
            log_calls.append((pid, text))

        db.finish_project_capsule = fake_finish
        db.log_project_capsule_event = fake_log
        reply = await by_name["finish_project_capsule"].coroutine(project_id=9, outcome_summary="Refund received.")
        check("finish_project_capsule tool: calls db.finish_project_capsule",
              finish_calls == [(1, 9, "Refund received.")])
        check("finish_project_capsule tool: does NOT also log a manual timeline event -- "
              "db.finish_project_capsule's own set_cron_job_status sync already does it exactly "
              "once (a second call here would double-log it, QA report Issue 2)",
              log_calls == [])

        # add_project_capsule_asset
        db.get_project_capsule = fake_get_with_cron
        asset_calls = []

        async def fake_add_asset(pid, url, desc):
            asset_calls.append((pid, url, desc))

        db.add_project_capsule_asset = fake_add_asset
        log_calls.clear()
        reply = await by_name["add_project_capsule_asset"].coroutine(
            project_id=9, source_url="https://cdn/receipt.jpg", description="Delta receipt",
        )
        check("add_project_capsule_asset tool: files the asset with the given url/description",
              asset_calls == [(9, "https://cdn/receipt.jpg", "Delta receipt")])
        check("add_project_capsule_asset tool: also logs a timeline event", bool(log_calls))

        # log_project_capsule_event tool
        log_calls.clear()
        reply = await by_name["log_project_capsule_event"].coroutine(project_id=9, event_text="Checked status page.")
        check("log_project_capsule_event tool: logs the given text",
              log_calls == [(9, "Checked status page.")])
    finally:
        db.list_project_capsules = real_list
        db.get_project_capsule = real_get
        db.list_project_capsule_assets = real_assets
        db.list_project_capsule_events = real_events
        db.set_cron_job_status = real_set_status
        db.add_project_capsule_asset = real_add_asset
        db.log_project_capsule_event = real_log_event
        db.finish_project_capsule = real_finish


# ---------------------------------------------------------------------------
# Part 7: PROJECT_CAPSULES_ENABLED kill switch
# ---------------------------------------------------------------------------

async def part7_kill_switch():
    real_flag = config.PROJECT_CAPSULES_ENABLED
    project_tool_names = {
        "propose_create_project_capsule", "list_my_project_capsules", "get_project_capsule_details",
        "pause_project_capsule", "resume_project_capsule", "cancel_project_capsule",
        "finish_project_capsule", "add_project_capsule_asset", "log_project_capsule_event",
    }
    try:
        config.PROJECT_CAPSULES_ENABLED = True
        tools_on = routines_tools.build_routines_tools(USER)
        names_on = {t.name for t in tools_on}
        check("kill switch on: every project-capsule tool is present", project_tool_names <= names_on)

        config.PROJECT_CAPSULES_ENABLED = False
        tools_off = routines_tools.build_routines_tools(USER)
        names_off = {t.name for t in tools_off}
        check("kill switch off: no project-capsule tool is exposed at all", not (project_tool_names & names_off))
        check("kill switch off: the ordinary routine tools are still present",
              "propose_create_routine" in names_off and "list_cron_jobs" in names_off)

        # The subagent's own description and system prompt also drop all
        # mention of project capsules when the flag is off.
        real_create_agent = routines_tools.create_agent
        captured = []

        def fake_create_agent(**kwargs):
            captured.append(kwargs)

            class _FakeInner:
                async def ainvoke(self, *a, **kw):
                    return {"messages": [AIMessage(content="ok")]}
            return _FakeInner()

        routines_tools.create_agent = fake_create_agent
        try:
            sub_off = routines_tools.build_routines_subagent(USER, object())
            check("kill switch off: subagent description omits 'PROJECT CAPSULES'",
                  "PROJECT CAPSULE" not in sub_off["description"])
            await sub_off["runnable"].ainvoke({"messages": [HumanMessage(content="hi")]})
            check("kill switch off: system prompt omits the project-capsules paragraph",
                  "PROJECT CAPSULE" not in captured[-1]["system_prompt"])

            config.PROJECT_CAPSULES_ENABLED = True
            sub_on = routines_tools.build_routines_subagent(USER, object())
            check("kill switch on: subagent description mentions PROJECT CAPSULES",
                  "PROJECT CAPSULE" in sub_on["description"])
            await sub_on["runnable"].ainvoke({"messages": [HumanMessage(content="hi")]})
            check("kill switch on: system prompt includes the project-capsules paragraph",
                  "PROJECT CAPSULE" in captured[-1]["system_prompt"])
        finally:
            routines_tools.create_agent = real_create_agent
    finally:
        config.PROJECT_CAPSULES_ENABLED = real_flag


# ---------------------------------------------------------------------------
# Part 8: registry.py wiring
# ---------------------------------------------------------------------------

async def part8_registry_wiring():
    # 8a: confirm_pending_action's shared create_routine/create_project
    # superseded-job branch.
    real_confirm = db.confirm_pending_action
    try:
        async def fake_confirm(uid, pid):
            return {
                "ok": True, "action_type": "create_project",
                "result": {"id": 9, "_superseded_job": {"id": 77, "prompt_or_task": "old task"}},
            }

        db.confirm_pending_action = fake_confirm
        tools = registry.build_orchestrator_tools(USER)
        confirm_tool = next(t for t in tools if t.name == "confirm_pending_action")
        reply = await confirm_tool.coroutine(pending_action_id=1)
        check("confirm_pending_action: 'create_project' also mentions a superseded routine",
              "cancelled routine #77" in reply)
    finally:
        db.confirm_pending_action = real_confirm

    # 8b: _active_project_capsules_paragraph
    paragraph = registry._active_project_capsules_paragraph([])
    check("_active_project_capsules_paragraph: empty list -> empty string", paragraph == "")

    paragraph = registry._active_project_capsules_paragraph(
        [{"id": 3, "title": "Delta refund"}, {"id": 4, "title": "Apartment search"}]
    )
    check("_active_project_capsules_paragraph: lists every active capsule's id and title",
          "#3 'Delta refund'" in paragraph and "#4 'Apartment search'" in paragraph)
    check("_active_project_capsules_paragraph: mentions the attachment-filing convention",
          "add_project_capsule_asset" in paragraph and "[attachment_url:" in paragraph)


# ---------------------------------------------------------------------------
# Part 9: server._fire_autonomous_routine's project-capsule-aware wrapped_task
# ---------------------------------------------------------------------------

async def part9_fire_autonomous_routine_project_context():
    """QA report Issue 3 (first bullet): the cadence loop's wrapped_task
    never told the model a firing cron job was actually driving a project
    capsule, so it had no way to call get_project_capsule_details/
    finish_project_capsule/log_project_capsule_event on itself. Confirms
    the fix: server._fire_autonomous_routine now looks up the linked
    capsule and branches its wrapped_task (and its expiry give-up message)
    accordingly, falling back to the original plain-routine wording
    whenever there isn't one (or it isn't 'active')."""
    real_get_capsule = db.get_project_capsule_by_cron_job
    real_load_user = server.cli.load_user_context
    real_build_orch = server.build_orchestrator
    real_run_message = server.cli.run_message
    real_list_jobs = db.list_cron_jobs
    real_reschedule = db.reschedule_cron_job
    real_set_status = db.set_cron_job_status
    real_send = server.sendblue.send_message

    captured_tasks = []
    status_calls = []
    send_calls = []

    async def fake_load_user_context(phone, channel="sms"):
        return object()

    async def fake_build_orchestrator(user, gate):
        return object()

    async def fake_run_message(user, agent, task, send=None):
        captured_tasks.append(task)

    async def fake_list_cron_jobs(uid):
        return [{"id": 501, "status": "active"}]

    async def fake_reschedule(cron_id, next_run, meta_patch=None):
        pass

    async def fake_set_status(uid, cron_id, status, meta_patch=None):
        status_calls.append((uid, cron_id, status, meta_patch))
        return {"id": cron_id}

    async def fake_send(phone, text):
        send_calls.append((phone, text))

    server.cli.load_user_context = fake_load_user_context
    server.build_orchestrator = fake_build_orchestrator
    server.cli.run_message = fake_run_message
    db.list_cron_jobs = fake_list_cron_jobs
    db.reschedule_cron_job = fake_reschedule
    db.set_cron_job_status = fake_set_status
    server.sendblue.send_message = fake_send

    job = {
        "id": 501, "user_id": 1, "phone_number": "+15551234567",
        "cron_expression": "0 9 * * *", "user_timezone": "UTC",
        "prompt_or_task": "dispute the $850 charge",
    }

    try:
        # 9a: linked, active project capsule -> project-aware wrapped_task.
        async def fake_get_active(cron_job_id):
            return {"id": 9, "title": "Delta refund", "status": "active"}

        db.get_project_capsule_by_cron_job = fake_get_active
        captured_tasks.clear()
        await server._fire_autonomous_routine(dict(job), {})
        task = captured_tasks[0]
        check("_fire_autonomous_routine: project-aware task names the capsule",
              "PROJECT CAPSULE #9" in task and "Delta refund" in task)
        check("_fire_autonomous_routine: project-aware task steers to get_project_capsule_details",
              "get_project_capsule_details" in task and "project_id=9" in task)
        check("_fire_autonomous_routine: project-aware task steers to finish_project_capsule",
              "finish_project_capsule" in task)
        check("_fire_autonomous_routine: project-aware task omits the plain-routine wording",
              "finish_routine" not in task and "cron_id=" not in task)

        # 9b: no linked capsule at all -> original plain-routine wording, unchanged.
        async def fake_get_none(cron_job_id):
            return None

        db.get_project_capsule_by_cron_job = fake_get_none
        captured_tasks.clear()
        await server._fire_autonomous_routine(dict(job), {})
        task = captured_tasks[0]
        check("_fire_autonomous_routine: plain routine task mentions finish_routine/cron_id",
              "finish_routine" in task and "cron_id=501" in task)
        check("_fire_autonomous_routine: plain routine task never mentions a project capsule",
              "PROJECT CAPSULE" not in task)

        # 9c: linked capsule but not active (e.g. already completed) -> falls
        # back to the plain-routine wording too, same as no capsule at all.
        async def fake_get_completed(cron_job_id):
            return {"id": 9, "title": "Delta refund", "status": "completed"}

        db.get_project_capsule_by_cron_job = fake_get_completed
        captured_tasks.clear()
        await server._fire_autonomous_routine(dict(job), {})
        task = captured_tasks[0]
        check("_fire_autonomous_routine: non-active linked capsule still falls back to plain wording",
              "finish_routine" in task and "PROJECT CAPSULE" not in task)

        # 9d: expiry give-up message names the project when it's active.
        db.get_project_capsule_by_cron_job = fake_get_active
        send_calls.clear()
        status_calls.clear()
        expired_meta = {"expire_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()}
        await server._fire_autonomous_routine(dict(job), dict(expired_meta))
        check("_fire_autonomous_routine: expiry message names the active project by title",
              send_calls and "Delta refund" in send_calls[0][1])
        check("_fire_autonomous_routine: expiry still cancels with ended_reason=expired",
              status_calls == [(1, 501, "cancelled", {"ended_reason": "expired"})])

        # 9e: expiry give-up message for a plain routine (no linked capsule) is unchanged.
        db.get_project_capsule_by_cron_job = fake_get_none
        send_calls.clear()
        await server._fire_autonomous_routine(dict(job), dict(expired_meta))
        check("_fire_autonomous_routine: plain-routine expiry message keeps the original wording",
              send_calls and "dispute the $850 charge" in send_calls[0][1]
              and "Delta refund" not in send_calls[0][1])
    finally:
        db.get_project_capsule_by_cron_job = real_get_capsule
        server.cli.load_user_context = real_load_user
        server.build_orchestrator = real_build_orch
        server.cli.run_message = real_run_message
        db.list_cron_jobs = real_list_jobs
        db.reschedule_cron_job = real_reschedule
        db.set_cron_job_status = real_set_status
        server.sendblue.send_message = real_send


async def main() -> None:
    await part1_insert_project_capsule()
    await part2_set_cron_job_status_sync()
    await part3_other_db_functions()
    await part4_build_schedule_and_meta_via_routine()
    await part5_propose_create_project_capsule()
    await part6_project_tools()
    await part7_kill_switch()
    await part8_registry_wiring()
    await part9_fire_autonomous_routine_project_context()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
