"""Tests for PR4's webhook route (plans/glowing-forging-pumpkin.md):
POST /webhook/vapi in messa/server.py, exercised end-to-end via
starlette's TestClient (same technique test_privacy_page.py's part4
already uses for a route in this same server.py), with only the DB layer
(and, for part 3, cli.load_user_context_by_id / usage.check_and_consume_
monthly) faked out -- everything from the HTTP request down through
call_tools' actual dispatch/allowlist logic runs for real.

Three parts:
  1. Secret rejection -- config.VAPI_WEBHOOK_SECRET set: no header or a
     wrong header is rejected with 401; the correct header is accepted.
     Unset secret: any request is accepted (matches Sendblue's own
     local-dev posture).
  2. Mid-call tool-call dispatch, end-to-end through the real HTTP route:
     an allowed field is served with its real value; a field NOT in the
     allowlist is rejected with the generic message, never a stack trace
     or the raw snapshot; an unknown provider_call_id is rejected.
  3. End-of-call-report, end-to-end through the real HTTP route: drives
     usage.check_and_consume_monthly with the real billed-minutes amount
     (ceiling-rounded from durationSeconds) and finalizes the
     call_sessions row (status='ended', outcome, scrubbed outcome_summary,
     no raw card/SSN text ever written to it even if Vapi's own summary
     field happened to include one).
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
    os.environ.setdefault("COMPOSIO_API_KEY", "comp-test-dummy-not-real")
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

from starlette.testclient import TestClient  # noqa: E402

from messa import call_activity, call_control, cli, config, db, usage  # noqa: E402
from messa.server import app  # noqa: E402
from messa.tools import call_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


class FakeConn:
    def __init__(self, fetchrow_queue=None):
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.calls = []

    async def fetchval(self, query, *args):
        return True

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.fetchrow_queue:
            return self.fetchrow_queue.pop(0)
        return None

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 1"


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
    pass


def install_fake_pool(conn):
    async def fake_get_pool():
        return FakePool(conn)
    db.get_pool = fake_get_pool


# ---------------------------------------------------------------------------
# Part 1: secret rejection
# ---------------------------------------------------------------------------

def part1_secret_rejection():
    real_secret = config.VAPI_WEBHOOK_SECRET
    client = TestClient(app)
    try:
        config.VAPI_WEBHOOK_SECRET = "shh-secret"
        conn = FakeConn(fetchrow_queue=[None])
        install_fake_pool(conn)

        resp_no_header = client.post("/webhook/vapi", json={"message": {"type": "status-update"}})
        check("no secret header: rejected with 401", resp_no_header.status_code == 401)

        resp_wrong = client.post(
            "/webhook/vapi", json={"message": {"type": "status-update"}},
            headers={"x-vapi-secret": "wrong-secret"},
        )
        check("wrong secret header: rejected with 401", resp_wrong.status_code == 401)

        resp_right = client.post(
            "/webhook/vapi", json={"message": {"type": "status-update"}},
            headers={"x-vapi-secret": "shh-secret"},
        )
        check("correct secret header: accepted (200)", resp_right.status_code == 200)
        check("an unrecognized event type is acknowledged, not treated as an error",
              resp_right.json().get("status") == "ignored")

        # Unset secret -- any request accepted, matching Sendblue's own
        # local-dev posture.
        config.VAPI_WEBHOOK_SECRET = None
        resp_unset = client.post("/webhook/vapi", json={"message": {"type": "status-update"}})
        check("unset secret: any request is accepted", resp_unset.status_code == 200)
    finally:
        config.VAPI_WEBHOOK_SECRET = real_secret


# ---------------------------------------------------------------------------
# Part 2: mid-call tool-call dispatch, end-to-end
# ---------------------------------------------------------------------------

def part2_mid_call_dispatch_end_to_end():
    real_secret = config.VAPI_WEBHOOK_SECRET
    config.VAPI_WEBHOOK_SECRET = None  # isolate this part from part1's auth concerns
    client = TestClient(app)
    call_tools._info_tool_call_counts.clear()

    call_row = FakeRow(
        call_id="c1", user_id=7, provider_call_id="vapi-call-1",
        allowed_info_fields=json.dumps(["usual_order"]),
        scratchpad_snapshot=json.dumps({"task_description": "order a latte", "usual_order": "grande oat milk latte"}),
    )

    def tool_call_body(provider_call_id, field):
        return {
            "message": {
                "type": "tool-calls",
                "call": {"id": provider_call_id},
                "toolCallId": "tc-1",
                "toolCalls": [{"id": "tc-1", "function": {"name": "get_call_info", "arguments": {"field": field}}}],
            },
        }

    try:
        # Allowed field -- served for real, through the whole HTTP route.
        conn = FakeConn(fetchrow_queue=[call_row])
        install_fake_pool(conn)
        resp = client.post("/webhook/vapi", json=tool_call_body("vapi-call-1", "usual_order"))
        check("mid-call dispatch: HTTP 200", resp.status_code == 200)
        body = resp.json()
        check("mid-call dispatch: an allowed field is served with its real value",
              body["results"][0]["result"] == "grande oat milk latte")

        # Field not in the allowlist -- rejected with the generic message,
        # never the raw snapshot or a stack trace.
        call_tools._info_tool_call_counts.clear()
        conn2 = FakeConn(fetchrow_queue=[call_row])
        install_fake_pool(conn2)
        resp2 = client.post("/webhook/vapi", json=tool_call_body("vapi-call-1", "loyalty_number"))
        body2 = resp2.json()
        check("mid-call dispatch: a disallowed field gets the generic decline",
              "isn't available" in body2["results"][0]["result"].lower())
        check("mid-call dispatch: a disallowed field's response never leaks the snapshot",
              "grande" not in body2["results"][0]["result"].lower())

        # Unknown provider_call_id -- rejected outright.
        conn3 = FakeConn(fetchrow_queue=[None])
        install_fake_pool(conn3)
        resp3 = client.post("/webhook/vapi", json=tool_call_body("not-ours", "usual_order"))
        body3 = resp3.json()
        check("mid-call dispatch: an unknown provider_call_id is rejected",
              "isn't available" in body3["results"][0]["result"].lower())
    finally:
        config.VAPI_WEBHOOK_SECRET = real_secret
        call_tools._info_tool_call_counts.clear()


# ---------------------------------------------------------------------------
# Part 3: end-of-call-report, end-to-end
# ---------------------------------------------------------------------------

def part3_end_of_call_report_end_to_end():
    real_secret = config.VAPI_WEBHOOK_SECRET
    config.VAPI_WEBHOOK_SECRET = None
    real_load_user = cli.load_user_context_by_id
    real_consume_monthly = usage.check_and_consume_monthly
    real_update = db.update_call_session
    client = TestClient(app)

    call_row = FakeRow(call_id="c1", user_id=7, provider_call_id="vapi-call-1")
    update_calls = []
    consume_calls = []

    async def fake_load_user(uid, channel="sms"):
        return config.UserContext(
            user_id=uid, phone_number="+15551234567", plan_id="plus",
            timezone="America/New_York", timezone_confirmed=True,
        )

    async def fake_consume_monthly(user, feature, amount=1):
        consume_calls.append((user.user_id, feature, amount))
        from messa.usage import LimitResult
        return LimitResult(allowed=True, feature=feature, count=amount, limit=20, plan_name="Plus")

    async def fake_update_call_session(call_id, fields):
        update_calls.append((call_id, dict(fields)))
        return {"call_id": call_id, **fields}

    cli.load_user_context_by_id = fake_load_user
    usage.check_and_consume_monthly = fake_consume_monthly
    db.update_call_session = fake_update_call_session

    try:
        conn = FakeConn(fetchrow_queue=[call_row])
        install_fake_pool(conn)

        # 125 seconds -> ceil(125/60) = 3 billed minutes.
        body = {
            "message": {
                "type": "end-of-call-report",
                "call": {"id": "vapi-call-1"},
                "durationSeconds": 125,
                "endedReason": "customer-ended-call",
                "summary": "Customer ordered a latte. Card on file: 4111111111111111.",
            },
        }
        resp = client.post("/webhook/vapi", json=body)
        check("end-of-call-report: HTTP 200", resp.status_code == 200)
        check("end-of-call-report: meters the real ceil-rounded billed minutes (125s -> 3min)",
              consume_calls and consume_calls[0][2] == 3)
        check("end-of-call-report: meters the right feature", consume_calls and consume_calls[0][1] == "call_minutes")

        finalize_fields = {}
        for call_id, fields in update_calls:
            finalize_fields.update(fields)
        check("end-of-call-report: marks the row ended", finalize_fields.get("status") == "ended")
        check("end-of-call-report: records duration_seconds", finalize_fields.get("duration_seconds") == 125)
        check("end-of-call-report: records minutes_billed", finalize_fields.get("minutes_billed") == 3)
        check("end-of-call-report: records a success outcome for a customer-ended call",
              finalize_fields.get("outcome") == "success")
        outcome_summary = finalize_fields.get("outcome_summary") or ""
        check("end-of-call-report: the stored summary never contains the raw card number "
              "(scrubbed even though Vapi's OWN summary text included one)",
              "4111111111111111" not in outcome_summary)

        # Unknown provider_call_id -- rejected, no metering/finalization happens.
        consume_calls.clear()
        update_calls.clear()
        conn2 = FakeConn(fetchrow_queue=[None])
        install_fake_pool(conn2)
        body_unknown = dict(body)
        body_unknown["message"] = dict(body["message"], call={"id": "not-ours"})
        resp2 = client.post("/webhook/vapi", json=body_unknown)
        check("end-of-call-report for an unknown call: HTTP 200 (acknowledged, not an error)",
              resp2.status_code == 200)
        check("end-of-call-report for an unknown call: no usage is metered", consume_calls == [])
        check("end-of-call-report for an unknown call: no row is finalized", update_calls == [])
    finally:
        config.VAPI_WEBHOOK_SECRET = real_secret
        cli.load_user_context_by_id = real_load_user
        usage.check_and_consume_monthly = real_consume_monthly
        db.update_call_session = real_update


def main() -> None:
    part1_secret_rejection()
    part2_mid_call_dispatch_end_to_end()
    part3_end_of_call_report_end_to_end()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
