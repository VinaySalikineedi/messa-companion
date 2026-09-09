"""Tests for PR1 of the voice-calling feature (plans/glowing-forging-pumpkin.md):
the data model and plans/usage plumbing that everything else in that plan is
built on top of, with nothing wired in yet that can actually place a call.

Five parts:
  1. messa/plans.py -- PlanLimits.call_minutes exists on all four shipped
     tiers with the agreed mapping (basic=0, pro=0, plus=20, business=40),
     and _parse_override's backward-compatibility guarantee: an override
     JSON written before this field existed (no "call_minutes" key at all)
     must NOT raise -- it must default to 0, not crash the whole app at
     import time.
  2. messa/timeutil.py -- local_month_start truncates to the first of the
     current month, in the given IANA zone, with the same UTC fallback
     local_today already uses when no zone is given.
  3. messa/usage.py -- check_and_consume_monthly/peek_usage_monthly against
     a fake db.check_and_increment_usage/get_usage_count, confirming they
     bucket by MONTH (not day), pass through a real `amount` (billed
     minutes, not always 1), and respect the same admin-always-allowed
     override check_and_consume itself has.
  4. messa/db.py -- the six new call_sessions CRUD functions against a fake
     asyncpg pool: the pre-migration graceful degrade (_has_table=False),
     and the normal create/get/get-by-provider-id/get-active/update/list
     lifecycle.
  5. messa/db.py -- update_call_session's generic partial-update actually
     builds the SQL SET clause from exactly the fields given, in order,
     with the call_id anchoring the WHERE clause as $1 (a real bug class:
     an off-by-one in that placeholder numbering would silently write the
     wrong value into the wrong column).

No live Postgres, no live LLM call, no real Vapi credentials -- none of
that exists yet at this stage of the rollout (see the plan's own PR1
scope: "nothing calls any of it yet").
"""
import asyncio
import json
import os
import sys

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

from messa import config, db, plans, timeutil, usage  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Fakes -- same FakeConn/FakeAcquire/FakePool shape as
# tests/test_scratchpad_and_skills.py / test_app_connect_queue.py.
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, fetch_queue=None, execute_results=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.fetch_queue = list(fetch_queue or [])
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
        if self.fetch_queue:
            return self.fetch_queue.pop(0)
        return []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.execute_results:
            return self.execute_results.pop(0)
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


def make_user(**overrides):
    kwargs = dict(
        user_id=1, phone_number="+15551234567", plan_id="plus",
        timezone="America/New_York", timezone_confirmed=True, is_admin=False,
    )
    kwargs.update(overrides)
    return config.UserContext(**kwargs)


# ---------------------------------------------------------------------------
# Part 1: plans.py
# ---------------------------------------------------------------------------

def part1_plans():
    check("basic.call_minutes == 0", plans.PLANS["basic"].limits.call_minutes == 0)
    check("pro.call_minutes == 0", plans.PLANS["pro"].limits.call_minutes == 0)
    check("plus.call_minutes == 20", plans.PLANS["plus"].limits.call_minutes == 20)
    check("business.call_minutes == 40", plans.PLANS["business"].limits.call_minutes == 40)

    # Backward compatibility: an override JSON written before call_minutes
    # existed (no key at all) must default to 0, never raise.
    legacy_override = json.dumps({
        "basic": {
            "name": "Basic", "price_cents": 0,
            "limits": {
                "outbound_emails": 10, "browse_actions": 5,
                "number_of_texts": 25, "max_connected_apps": 2,
                # no "call_minutes" key -- this is the whole point of the test
            },
        },
    })
    try:
        parsed = plans._parse_override(legacy_override)
        check("legacy override (no call_minutes key) parses without raising", True)
        check("legacy override defaults call_minutes to 0", parsed["basic"].limits.call_minutes == 0)
    except Exception as e:  # noqa: BLE001
        check(f"legacy override (no call_minutes key) parses without raising (raised: {e})", False)

    # An override that DOES specify call_minutes is honored as given.
    modern_override = json.dumps({
        "basic": {
            "name": "Basic", "price_cents": 0,
            "limits": {
                "outbound_emails": 10, "browse_actions": 5,
                "number_of_texts": 25, "max_connected_apps": 2, "call_minutes": 15,
            },
        },
    })
    parsed2 = plans._parse_override(modern_override)
    check("override with explicit call_minutes is honored", parsed2["basic"].limits.call_minutes == 15)


# ---------------------------------------------------------------------------
# Part 2: timeutil.local_month_start
# ---------------------------------------------------------------------------

def part2_local_month_start():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    result = timeutil.local_month_start("America/New_York")
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    check("local_month_start is day 1", result.day == 1)
    check("local_month_start is the current month", result.month == now_ny.month)
    check("local_month_start is the current year", result.year == now_ny.year)

    # No timezone given -- falls back to UTC, same as local_today.
    from datetime import timezone as dt_timezone
    result_utc = timeutil.local_month_start(None)
    now_utc = datetime.now(dt_timezone.utc)
    check("local_month_start(None) falls back to UTC and is day 1", result_utc.day == 1)
    check("local_month_start(None) matches UTC's current month", result_utc.month == now_utc.month)


# ---------------------------------------------------------------------------
# Part 3: usage.py monthly helpers
# ---------------------------------------------------------------------------

async def part3_usage_monthly():
    real_check_and_increment = db.check_and_increment_usage
    real_get_usage_count = db.get_usage_count
    calls = []

    async def fake_check_and_increment(user_id, feature, day, limit, amount=1):
        calls.append(("increment", user_id, feature, day, limit, amount))
        # Simulate 5 minutes already used this month, plus this call's amount.
        count_after = 5 + amount
        return (limit is None or count_after <= limit), count_after

    async def fake_get_usage_count(user_id, feature, day):
        calls.append(("peek", user_id, feature, day))
        return 5

    db.check_and_increment_usage = fake_check_and_increment
    db.get_usage_count = fake_get_usage_count

    try:
        user = make_user(plan_id="plus")  # call_minutes=20

        # check_and_consume_monthly: bucketed by MONTH, real variable amount.
        calls.clear()
        result = await usage.check_and_consume_monthly(user, usage.FEATURE_CALL_MINUTES, amount=3)
        check("check_and_consume_monthly calls check_and_increment_usage once", len(calls) == 1)
        kind, uid, feature, day, limit, amount = calls[0]
        check("check_and_consume_monthly passes feature='call_minutes'", feature == "call_minutes")
        check("check_and_consume_monthly passes the real amount (3), not 1", amount == 3)
        check("check_and_consume_monthly passes a month-START date (day == 1)", day.day == 1)
        check("check_and_consume_monthly passes the plan's limit (20)", limit == 20)
        check("check_and_consume_monthly result.count reflects the increment (5+3=8)", result.count == 8)
        check("check_and_consume_monthly result.allowed is True (8 <= 20)", result.allowed is True)

        # peek_usage_monthly: read-only, no increment call at all.
        calls.clear()
        peek = await usage.peek_usage_monthly(user, usage.FEATURE_CALL_MINUTES)
        check("peek_usage_monthly calls get_usage_count exactly once", len(calls) == 1)
        if calls:
            peek_kind, peek_uid, peek_feature, peek_day = calls[0]
            check("peek_usage_monthly calls the 'peek' path, not 'increment'", peek_kind == "peek")
            check("peek_usage_monthly passes feature='call_minutes'", peek_feature == "call_minutes")
            check("peek_usage_monthly passes a month-START date (day == 1)", peek_day.day == 1)
        check("peek_usage_monthly returns the count unmodified (5)", peek.count == 5)

        # Admin override: always allowed regardless of the underlying count.
        calls.clear()
        admin_user = make_user(plan_id="basic", is_admin=True)  # basic.call_minutes == 0
        admin_result = await usage.check_and_consume_monthly(admin_user, usage.FEATURE_CALL_MINUTES, amount=10)
        check("admin is always allowed even on a 0-minute plan", admin_result.allowed is True)

        # A plan with call_minutes=0 (basic) is NOT allowed for a real user.
        calls.clear()

        async def fake_zero_increment(user_id, feature, day, limit, amount=1):
            count_after = amount
            return (limit is None or count_after <= limit), count_after

        db.check_and_increment_usage = fake_zero_increment
        basic_user = make_user(plan_id="basic", is_admin=False)
        basic_result = await usage.check_and_consume_monthly(basic_user, usage.FEATURE_CALL_MINUTES, amount=1)
        check("a non-admin user on the basic plan (call_minutes=0) is not allowed",
              basic_result.allowed is False)
    finally:
        db.check_and_increment_usage = real_check_and_increment
        db.get_usage_count = real_get_usage_count


# ---------------------------------------------------------------------------
# Part 4: db.py call_sessions CRUD
# ---------------------------------------------------------------------------

async def part4_call_sessions_pre_migration_degrade():
    conn = FakeConn(has_tables=False)
    install_fake_pool(conn)

    check("create_call_session returns None pre-migration",
          await db.create_call_session(
              1, call_id="c1", destination_number="+15551234567",
              task_description="order a latte", max_duration_seconds=600,
          ) is None)
    check("get_call_session returns None pre-migration", await db.get_call_session("c1") is None)
    check("get_call_session_by_provider_id returns None pre-migration",
          await db.get_call_session_by_provider_id("vapi-123") is None)
    check("get_active_call_session_for_user returns None pre-migration",
          await db.get_active_call_session_for_user(1) is None)
    check("update_call_session returns None pre-migration",
          await db.update_call_session("c1", {"status": "dialing"}) is None)
    check("list_recent_call_sessions returns [] pre-migration",
          await db.list_recent_call_sessions(1) == [])


async def part4_call_sessions_lifecycle():
    # create_call_session
    conn = FakeConn(has_tables=True, fetchrow_queue=[
        FakeRow(id=1, call_id="c1", user_id=1, status="confirmed", destination_number="+15551234567"),
    ])
    install_fake_pool(conn)
    created = await db.create_call_session(
        1, call_id="c1", destination_number="+15551234567",
        task_description="order a latte", max_duration_seconds=600,
        business_name="Starbucks",
    )
    check("create_call_session returns the inserted row", created is not None and created["call_id"] == "c1")
    insert_call = conn.calls[0]
    check("create_call_session issues one fetchrow (INSERT ... RETURNING)", insert_call[0] == "fetchrow")
    check("create_call_session's INSERT mentions call_sessions", "call_sessions" in insert_call[1])

    # get_call_session
    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(call_id="c1", status="confirmed")])
    install_fake_pool(conn)
    got = await db.get_call_session("c1")
    check("get_call_session returns the row", got is not None and got["call_id"] == "c1")
    check("get_call_session queries by call_id", conn.calls[0][2] == ("c1",))

    # get_call_session_by_provider_id
    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(call_id="c1", provider_call_id="vapi-123")])
    install_fake_pool(conn)
    got2 = await db.get_call_session_by_provider_id("vapi-123")
    check("get_call_session_by_provider_id returns the row",
          got2 is not None and got2["provider_call_id"] == "vapi-123")

    # get_call_session_by_provider_id -- unknown id -> None (defends against
    # a forged/replayed webhook, see call_tools.py's security notes).
    conn = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn)
    got3 = await db.get_call_session_by_provider_id("not-ours")
    check("get_call_session_by_provider_id returns None for an unknown provider id", got3 is None)

    # get_active_call_session_for_user
    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(call_id="c1", status="in_progress")])
    install_fake_pool(conn)
    active = await db.get_active_call_session_for_user(1)
    check("get_active_call_session_for_user returns the in-flight row",
          active is not None and active["status"] == "in_progress")
    check("get_active_call_session_for_user excludes terminal states in its query",
          "NOT IN ('ended', 'failed')" in conn.calls[0][1])

    # list_recent_call_sessions
    conn = FakeConn(has_tables=True, fetch_queue=[[FakeRow(call_id="c1"), FakeRow(call_id="c2")]])
    install_fake_pool(conn)
    recent = await db.list_recent_call_sessions(1, limit=5)
    check("list_recent_call_sessions returns both rows", len(recent) == 2)
    check("list_recent_call_sessions passes the limit through", conn.calls[0][2] == (1, 5))


# ---------------------------------------------------------------------------
# Part 5: update_call_session -- SET-clause construction correctness
# ---------------------------------------------------------------------------

async def part5_update_call_session_set_clause():
    conn = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(call_id="c1", status="ended")])
    install_fake_pool(conn)

    result = await db.update_call_session("c1", {"status": "ended", "duration_seconds": 42})
    check("update_call_session returns the updated row", result is not None and result["status"] == "ended")

    query, args = conn.calls[0][1], conn.calls[0][2]
    check("update_call_session's query sets status = $2", "status = $2" in query)
    check("update_call_session's query sets duration_seconds = $3", "duration_seconds = $3" in query)
    check("update_call_session's WHERE anchors call_id as $1", "WHERE call_id = $1" in query)
    check("update_call_session's query bumps updated_at", "updated_at = NOW()" in query)
    # args[0] is call_id ($1), args[1]/args[2] are the field values in the
    # SAME order the fields dict was given -- an off-by-one here would
    # silently write the wrong value into the wrong column.
    check("update_call_session's positional args are (call_id, status_value, duration_value)",
          args == ("c1", "ended", 42))

    # Empty fields dict -- must not build a malformed "SET ," query; falls
    # back to a plain get.
    conn2 = FakeConn(has_tables=True, fetchrow_queue=[FakeRow(call_id="c1", status="ended")])
    install_fake_pool(conn2)
    result2 = await db.update_call_session("c1", {})
    check("update_call_session with no fields falls back to a plain get, no crash",
          result2 is not None and result2["call_id"] == "c1")


async def main() -> None:
    part1_plans()
    part2_local_month_start()
    await part3_usage_monthly()
    await part4_call_sessions_pre_migration_degrade()
    await part4_call_sessions_lifecycle()
    await part5_update_call_session_set_clause()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
