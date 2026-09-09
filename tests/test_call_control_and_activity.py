"""Tests for PR3 of the voice-calling feature (plans/glowing-forging-pumpkin.md):
messa/call_control.py, messa/call_activity.py, and the 'place_call' gated-
action applier in messa/db.py. Nothing calls any of this from a real tool
or route yet (that's PR4-5) -- propose_call/dial_confirmed_call don't
exist until PR5.

Four parts:
  1. call_control.py -- register/unregister/active_count/is_active/
     describe, and the staleness GC: a registration older than its own
     max_duration_seconds + STALE_GRACE_SECONDS is pruned on the next read
     rather than staying pinned "active" forever (simulated by
     monkeypatching time.monotonic, not a real sleep).
  2. call_activity.py -- start/set_status/add_transcript_line (including
     the MAX_TRANSCRIPT_LINES cap)/set_listen_url/get/clear, and that
     get() never raises for an untracked user.
  3. db._insert_call_session_confirmed -- the 'place_call' applier: issues
     exactly one INSERT with status='confirmed' (never touches Vapi/any
     external call inside it), generates its own call_id, and is
     registered under db.GATED_ACTION_TYPES.
  4. db.confirm_pending_action's existing transactional flow actually
     reaches this new applier end to end for a 'place_call' action (fake
     pool/conn, same shape as the calendar-event/broadcast tests this
     mirrors).
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

from messa import call_activity, call_control, db  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Fakes -- same FakeConn/FakeAcquire/FakePool shape as
# tests/test_call_plans_usage.py / test_scratchpad_and_skills.py.
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, execute_results=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.execute_results = list(execute_results or [])
        self.calls = []

    async def fetchval(self, query, *args):
        return self.has_tables

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.fetchrow_queue:
            return self.fetchrow_queue.pop(0)
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.execute_results:
            return self.execute_results.pop(0)
        return "UPDATE 0"

    def transaction(self):
        return _FakeTransaction()


class _FakeTransaction:
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
# Part 1: call_control.py
# ---------------------------------------------------------------------------

def part1_call_control():
    call_control.clear_all_for_test()

    check("a fresh user has no active calls", call_control.active_count(1) == 0)
    check("a fresh user is_active is False", call_control.is_active(1) is False)

    call_control.register(1, "call-a", max_duration_seconds=600)
    check("active_count is 1 after one register", call_control.active_count(1) == 1)
    check("is_active is True after one register", call_control.is_active(1) is True)
    check("describe lists the registered call_id", call_control.describe(1) == ["call-a"])

    call_control.register(1, "call-b", max_duration_seconds=600)
    check("active_count is 2 after a second register (different call_id)",
          call_control.active_count(1) == 2)

    call_control.unregister(1, "call-a")
    check("active_count drops to 1 after unregistering one", call_control.active_count(1) == 1)
    check("describe no longer lists the unregistered call", "call-a" not in call_control.describe(1))

    call_control.unregister(1, "call-b")
    check("active_count is 0 after unregistering the last one", call_control.active_count(1) == 0)
    check("is_active is False once the user has no calls left", call_control.is_active(1) is False)

    # unregister on something never registered / already gone -- no crash.
    call_control.unregister(1, "never-existed")
    check("unregistering an unknown call_id does not raise", True)

    # Users are independent.
    call_control.register(1, "call-c", max_duration_seconds=600)
    check("a different user is unaffected by user 1's registrations", call_control.active_count(2) == 0)
    call_control.clear_all_for_test()


def part1_call_control_staleness():
    call_control.clear_all_for_test()
    real_monotonic = time.monotonic
    try:
        fake_now = [1000.0]
        time.monotonic = lambda: fake_now[0]

        # A short call (60s max) registered at t=1000.
        call_control.register(1, "short-call", max_duration_seconds=60)
        check("freshly registered short call is active", call_control.is_active(1) is True)

        # Advance time past max_duration + STALE_GRACE_SECONDS -- must be pruned.
        fake_now[0] = 1000.0 + 60 + call_control.STALE_GRACE_SECONDS + 1
        check("a call past its max_duration+grace window is pruned (is_active False)",
              call_control.is_active(1) is False)
        check("active_count reflects the prune too", call_control.active_count(1) == 0)

        # A long call (600s max) registered at the new "now" is NOT stale yet.
        call_control.register(1, "long-call", max_duration_seconds=600)
        fake_now[0] += 30  # well within 600 + grace
        check("a call still within its own max_duration+grace window stays active",
              call_control.is_active(1) is True)
    finally:
        time.monotonic = real_monotonic
        call_control.clear_all_for_test()


# ---------------------------------------------------------------------------
# Part 2: call_activity.py
# ---------------------------------------------------------------------------

def part2_call_activity():
    call_activity.clear(99)  # ensure clean slate

    # An untracked user never raises, comes back as a sane empty default.
    empty = call_activity.get(99)
    check("get() for an untracked user does not raise and has status 'starting'",
          empty["status"] == "starting")
    check("get() for an untracked user has no call_id", empty["call_id"] is None)
    check("get() for an untracked user has an empty transcript", empty["transcript_lines"] == [])

    call_activity.start(99, call_id="c1", business_name="Starbucks", task_description="order a latte")
    entry = call_activity.get(99)
    check("start() sets call_id", entry["call_id"] == "c1")
    check("start() sets business_name", entry["business_name"] == "Starbucks")
    check("start() sets task_description", entry["task_description"] == "order a latte")
    check("start() sets an initial status", entry["status"] == "dialing")

    call_activity.set_status(99, "in progress")
    check("set_status updates the status", call_activity.get(99)["status"] == "in progress")

    call_activity.add_transcript_line(99, "Hi, I'd like to order a latte.")
    check("add_transcript_line appends a line", call_activity.get(99)["transcript_lines"] == [
        "Hi, I'd like to order a latte.",
    ])

    # Cap enforcement: adding more than MAX_TRANSCRIPT_LINES keeps only the
    # most recent ones.
    call_activity.clear(99)
    call_activity.start(99, call_id="c2", business_name=None, task_description="test")
    for i in range(call_activity.MAX_TRANSCRIPT_LINES + 25):
        call_activity.add_transcript_line(99, f"line {i}")
    lines = call_activity.get(99)["transcript_lines"]
    check("transcript is capped at MAX_TRANSCRIPT_LINES", len(lines) == call_activity.MAX_TRANSCRIPT_LINES)
    check("the cap keeps the MOST RECENT lines, not the oldest",
          lines[-1] == f"line {call_activity.MAX_TRANSCRIPT_LINES + 24}")

    # listen_url is held only here, never in the returned dict's absence.
    call_activity.set_listen_url(99, "wss://vapi.example/listen/abc123")
    check("set_listen_url is readable via get()",
          call_activity.get(99)["listen_url"] == "wss://vapi.example/listen/abc123")

    call_activity.clear(99)
    check("clear() resets to the untracked-user default", call_activity.get(99)["call_id"] is None)


# ---------------------------------------------------------------------------
# Part 3: db._insert_call_session_confirmed / _APPLIERS wiring
# ---------------------------------------------------------------------------

def part3_place_call_applier_registered():
    check("'place_call' is a key in db._APPLIERS", "place_call" in db._APPLIERS)
    check("'place_call' is in db.GATED_ACTION_TYPES", "place_call" in db.GATED_ACTION_TYPES)
    check("db._APPLIERS['place_call'] is the dedicated applier function",
          db._APPLIERS["place_call"] is db._insert_call_session_confirmed)


async def part3_insert_call_session_confirmed_shape():
    conn = FakeConn(fetchrow_queue=[
        FakeRow(id=1, call_id="whatever-uuid", user_id=7, status="confirmed",
                destination_number="+15551234567", business_name="Starbucks"),
    ])
    payload = {
        "destination_number": "+15551234567",
        "business_name": "Starbucks",
        "task_description": "order my usual: grande oat milk latte",
        "scratchpad_snapshot": '{"usual_order": "grande oat milk latte"}',
        "allowed_info_fields": '["usual_order"]',
        "max_duration_seconds": 300,
    }
    result = await db._insert_call_session_confirmed(conn, 7, payload)
    check("_insert_call_session_confirmed returns the inserted row", result["status"] == "confirmed")
    check("_insert_call_session_confirmed issues exactly one fetchrow (one INSERT)", len(conn.calls) == 1)
    kind, query, args = conn.calls[0]
    check("_insert_call_session_confirmed's query is an INSERT into call_sessions",
          "INSERT INTO call_sessions" in query)
    check("_insert_call_session_confirmed's query sets status='confirmed' literally (not a param)",
          "'confirmed'" in query)
    check("_insert_call_session_confirmed's query only ever names 'vapi' as the literal "
          "default provider value, never an actual API call/host",
          "api.vapi.ai" not in query.lower() and "create_call" not in query.lower())
    # args: (call_id, user_id, destination_number, business_name, task_description,
    #        scratchpad_snapshot, allowed_info_fields, max_duration_seconds)
    check("_insert_call_session_confirmed generates its own call_id (a non-empty string, not from payload)",
          isinstance(args[0], str) and len(args[0]) > 0)
    check("_insert_call_session_confirmed passes the real user_id", args[1] == 7)
    check("_insert_call_session_confirmed passes destination_number from payload",
          args[2] == "+15551234567")
    check("_insert_call_session_confirmed passes max_duration_seconds from payload", args[7] == 300)


# ---------------------------------------------------------------------------
# Part 4: confirm_pending_action end-to-end for a 'place_call' action
# ---------------------------------------------------------------------------

async def part4_confirm_pending_action_place_call():
    from datetime import datetime, timedelta, timezone
    import json as json_mod

    payload = {
        "destination_number": "+15551234567",
        "business_name": "Starbucks",
        "task_description": "order my usual",
        "scratchpad_snapshot": "{}",
        "allowed_info_fields": "[]",
        "max_duration_seconds": 300,
    }
    pending_row = FakeRow(
        id=42, user_id=7, action_type="place_call",
        payload=json_mod.dumps(payload), state="pending",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )
    inserted_row = FakeRow(id=1, call_id="c-generated", user_id=7, status="confirmed")

    conn = FakeConn(fetchrow_queue=[
        pending_row,   # get_pending_action's own SELECT
        inserted_row,  # the applier's INSERT ... RETURNING
    ])
    install_fake_pool(conn)

    result = await db.confirm_pending_action(7, 42)
    check("confirm_pending_action succeeds for a 'place_call' action", result.get("ok") is True)
    check("confirm_pending_action returns action_type='place_call'",
          result.get("action_type") == "place_call")
    check("confirm_pending_action returns the applier's result (the new call_sessions row)",
          result.get("result", {}).get("call_id") == "c-generated")

    # Two more calls should have happened inside the transaction: marking
    # pending_actions confirmed, and the audit_logs insert.
    executed_queries = [q for (kind, q, a) in conn.calls if kind == "execute"]
    check("confirm_pending_action marks the pending_actions row confirmed",
          any("pending_actions" in q and "confirmed" in q for q in executed_queries))
    check("confirm_pending_action writes an audit_logs row",
          any("audit_logs" in q for q in executed_queries))


async def main() -> None:
    part1_call_control()
    part1_call_control_staleness()
    part2_call_activity()
    part3_place_call_applier_registered()
    await part3_insert_call_session_confirmed_shape()
    await part4_confirm_pending_action_place_call()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
