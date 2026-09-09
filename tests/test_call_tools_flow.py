"""Tests for PR5's control flow (plans/glowing-forging-pumpkin.md):
messa/tools/call_tools.py's build_call_subagent gating, propose_call's
pending-action shape, and dial_confirmed_call (the one function that
actually calls Vapi, mocked here).

Four parts:
  1. build_call_subagent's gating branches, each verified to short-circuit
     BEFORE the inner create_agent() is ever built (same "invoke the
     closure with create_agent monkeypatched" technique
     test_scratchpad_and_skills.py's part10 already uses for the other
     CompiledSubAgents): not configured, wrong plan (call_minutes == 0),
     no has_call_access, fail-closed on a DB error, concurrency cap
     reached. Then one positive path: everything allowed reaches the
     inner agent.
  2. propose_call -- rejects an invalid destination before touching the
     DB at all; a valid call produces a correctly-shaped 'place_call'
     pending action (right payload keys, scrubbed snapshot, duration
     capped by remaining minutes).
  3. dial_confirmed_call -- calls vapi.create_call with the right
     arguments, updates the call_sessions row, registers with
     call_control/call_activity on success; marks the row failed (and
     never calls call_control.register) on a VapiError; refuses to dial
     at all if the concurrency cap was reached between confirm and dial.
  4. registry.py's confirm_pending_action tool actually reaches
     dial_confirmed_call for a 'place_call' action (structural check --
     confirming the wiring exists, mirroring test_scratchpad_and_skills.py
     part9's own structural-only checks for the heavier browser-based
     subagent).
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

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from messa import call_activity, call_control, config, db  # noqa: E402
from messa.channels import vapi  # noqa: E402
from messa.channels.vapi import VapiError  # noqa: E402
from messa.tools import call_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def make_user(**overrides):
    kwargs = dict(
        user_id=1, phone_number="+15551234567", plan_id="plus",
        timezone="America/New_York", timezone_confirmed=True, is_admin=False,
        call_beta_access=True,
    )
    kwargs.update(overrides)
    return config.UserContext(**kwargs)


class FakeInnerAgent:
    async def ainvoke(self, *a, **kw):
        return {"messages": [AIMessage(content="I'll call Starbucks now -- confirm?")]}


# ---------------------------------------------------------------------------
# Part 1: build_call_subagent gating
# ---------------------------------------------------------------------------

async def part1_gating():
    real_create_agent = call_tools.create_agent
    real_vapi_key, real_phone_id = config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID
    real_peek_fc = call_tools.usage.peek_usage_monthly_fail_closed
    real_active_count = call_control.active_count
    reached_create_agent = []

    def fake_create_agent(**kwargs):
        reached_create_agent.append(kwargs)
        return FakeInnerAgent()

    call_tools.create_agent = fake_create_agent

    try:
        # 1a. Not configured.
        config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID = None, None
        reached_create_agent.clear()
        user = make_user(plan_id="plus")
        sub = call_tools.build_call_subagent(user, model=object())
        result = await sub["runnable"].ainvoke({"messages": [HumanMessage(content="call starbucks")]})
        check("not configured: declines without reaching create_agent", reached_create_agent == [])
        check("not configured: the decline mentions it isn't set up yet",
              "set up" in result["messages"][0].content.lower())

        # 1b. Configured, but plan has call_minutes == 0.
        config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID = "key", "phone-1"
        reached_create_agent.clear()
        user_basic = make_user(plan_id="basic")
        sub_basic = call_tools.build_call_subagent(user_basic, model=object())
        result_basic = await sub_basic["runnable"].ainvoke({"messages": [HumanMessage(content="call starbucks")]})
        check("call_minutes == 0 plan: declines without reaching create_agent", reached_create_agent == [])
        check("call_minutes == 0 plan: the decline is plan/upgrade flavored",
              "plan" in result_basic["messages"][0].content.lower())

        # 1c. Configured, good plan, but no has_call_access.
        reached_create_agent.clear()
        user_no_access = make_user(plan_id="plus", call_beta_access=False, is_admin=False)
        sub_no_access = call_tools.build_call_subagent(user_no_access, model=object())
        result_no_access = await sub_no_access["runnable"].ainvoke({"messages": [HumanMessage(content="call starbucks")]})
        check("no has_call_access: declines without reaching create_agent", reached_create_agent == [])

        # 1d. has_call_access True, but the fail-closed usage check says no
        # (simulating a DB outage).
        async def fake_peek_fc_denied(user_arg, feature):
            from messa.usage import LimitResult
            return LimitResult(allowed=False, feature=feature, count=0, limit=20, plan_name="Plus")

        call_tools.usage.peek_usage_monthly_fail_closed = fake_peek_fc_denied
        reached_create_agent.clear()
        user_ok = make_user(plan_id="plus", call_beta_access=True)
        sub_denied = call_tools.build_call_subagent(user_ok, model=object())
        result_denied = await sub_denied["runnable"].ainvoke({"messages": [HumanMessage(content="call starbucks")]})
        check("fail-closed usage check denies: declines without reaching create_agent",
              reached_create_agent == [])
        call_tools.usage.peek_usage_monthly_fail_closed = real_peek_fc

        # 1e. Everything allowed except the concurrency cap is already at max.
        async def fake_peek_fc_allowed(user_arg, feature):
            from messa.usage import LimitResult
            return LimitResult(allowed=True, feature=feature, count=0, limit=20, plan_name="Plus")

        call_tools.usage.peek_usage_monthly_fail_closed = fake_peek_fc_allowed
        call_control.active_count = lambda uid: config.CALL_MAX_CONCURRENT_PER_USER  # already at the cap
        reached_create_agent.clear()
        sub_cap = call_tools.build_call_subagent(user_ok, model=object())
        result_cap = await sub_cap["runnable"].ainvoke({"messages": [HumanMessage(content="call starbucks")]})
        check("concurrency cap reached: declines without reaching create_agent", reached_create_agent == [])
        call_control.active_count = real_active_count

        # 1f. Positive path: everything allowed reaches the inner agent.
        reached_create_agent.clear()
        sub_ok = call_tools.build_call_subagent(user_ok, model=object())
        result_ok = await sub_ok["runnable"].ainvoke({"messages": [HumanMessage(content="call starbucks")]})
        check("everything allowed: reaches create_agent exactly once", len(reached_create_agent) == 1)
        check("everything allowed: the inner agent's reply is relayed",
              "starbucks" in result_ok["messages"][0].content.lower())
    finally:
        call_tools.create_agent = real_create_agent
        config.VAPI_API_KEY, config.VAPI_PHONE_NUMBER_ID = real_vapi_key, real_phone_id
        call_tools.usage.peek_usage_monthly_fail_closed = real_peek_fc
        call_control.active_count = real_active_count


# ---------------------------------------------------------------------------
# Part 2: propose_call
# ---------------------------------------------------------------------------

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
        return "INSERT 0 1"


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


async def part2_propose_call():
    real_peek_fc = call_tools.usage.peek_usage_monthly_fail_closed

    async def fake_peek_fc(user_arg, feature):
        from messa.usage import LimitResult
        return LimitResult(allowed=True, feature=feature, count=5, limit=20, plan_name="Plus")

    call_tools.usage.peek_usage_monthly_fail_closed = fake_peek_fc
    try:
        user = make_user(plan_id="plus")
        tools = call_tools.build_call_tools(user)
        propose_call_tool = next(t for t in tools if t.name == "propose_call")

        # Invalid destination -- rejected before touching the DB.
        conn = FakeConn()
        install_fake_pool(conn)
        result_invalid = await propose_call_tool.ainvoke({
            "destination_number": "555-1234", "business_name": "Starbucks",
            "task_description": "order a latte",
        })
        check("propose_call rejects an invalid destination before any DB call",
              conn.calls == [] and "valid phone number" in result_invalid.lower())

        # Valid call.
        conn2 = FakeConn(fetchrow_queue=[FakeRow(id=99, user_id=1, action_type="place_call")])
        install_fake_pool(conn2)
        result_ok = await propose_call_tool.ainvoke({
            "destination_number": "+15551234567", "business_name": "Starbucks",
            "task_description": "order my usual",
            "info_fields": {"usual_order": "grande oat milk latte", "cvv": "999"},
        })
        check("propose_call succeeds for a valid destination", "starbucks" in result_ok.lower())
        check("propose_call issues exactly one propose_action INSERT", len(conn2.calls) == 1)
        query, args = conn2.calls[0][1], conn2.calls[0][2]
        check("propose_call's INSERT targets pending_actions", "pending_actions" in query)
        check("propose_call's action_type is 'place_call'", args[1] == "place_call")
        payload = json.loads(args[2])
        check("propose_call's payload carries the destination number", payload["destination_number"] == "+15551234567")
        check("propose_call's payload carries the business name", payload["business_name"] == "Starbucks")
        snapshot = json.loads(payload["scratchpad_snapshot"])
        check("propose_call's snapshot includes the safe info field", snapshot.get("usual_order") == "grande oat milk latte")
        check("propose_call's snapshot excludes the sensitive field", "cvv" not in snapshot)
        allowed_fields = json.loads(payload["allowed_info_fields"])
        check("propose_call's allowed_info_fields excludes the sensitive field", "cvv" not in allowed_fields)
        # remaining minutes = 20 - 5 = 15 -> 900s, vs config default 600s -- min() should win with 600.
        check("propose_call caps max_duration_seconds at config.CALL_MAX_DURATION_SECONDS when it's the smaller value",
              payload["max_duration_seconds"] == config.CALL_MAX_DURATION_SECONDS)
    finally:
        call_tools.usage.peek_usage_monthly_fail_closed = real_peek_fc


# ---------------------------------------------------------------------------
# Part 3: dial_confirmed_call
# ---------------------------------------------------------------------------

async def part3_dial_confirmed_call():
    real_create_call = vapi.create_call
    real_active_count = call_control.active_count
    real_get_token = db.get_or_create_live_share_token

    async def fake_get_token(uid):
        return "tok123"

    db.get_or_create_live_share_token = fake_get_token

    call_row = {
        "call_id": "c-1", "user_id": 42, "destination_number": "+15551234567",
        "business_name": "Starbucks", "task_description": "order my usual",
        "allowed_info_fields": json.dumps(["usual_order"]),
        "scratchpad_snapshot": json.dumps({"usual_order": "grande oat milk latte"}),
        "max_duration_seconds": 300,
    }

    try:
        # Success path.
        update_calls = []
        real_update = db.update_call_session

        async def fake_update(call_id, fields):
            update_calls.append((call_id, dict(fields)))
            return {"call_id": call_id, **fields}

        db.update_call_session = fake_update
        call_control.active_count = lambda uid: 0  # under the cap

        create_call_args = {}

        async def fake_create_call(destination_number, *, assistant, metadata, max_duration_seconds):
            create_call_args["destination_number"] = destination_number
            create_call_args["assistant"] = assistant
            create_call_args["metadata"] = metadata
            create_call_args["max_duration_seconds"] = max_duration_seconds
            return {"id": "vapi-call-99", "listenUrl": "wss://vapi.example/listen/xyz"}

        vapi.create_call = fake_create_call
        call_control.clear_all_for_test()
        call_activity.clear(42)

        outcome = await call_tools.dial_confirmed_call(call_row)
        check("dial_confirmed_call reaches vapi.create_call with the right destination",
              create_call_args["destination_number"] == "+15551234567")
        check("dial_confirmed_call passes call_session_id in metadata",
              create_call_args["metadata"] == {"call_session_id": "c-1"})
        check("dial_confirmed_call passes the row's own max_duration_seconds",
              create_call_args["max_duration_seconds"] == 300)
        check("dial_confirmed_call updates the row with the provider_call_id",
              any(f.get("provider_call_id") == "vapi-call-99" for _, f in update_calls))
        check("dial_confirmed_call registers the call with call_control",
              call_control.is_active(42) is True)
        check("dial_confirmed_call starts call_activity for this user",
              call_activity.get(42)["call_id"] == "c-1")
        check("dial_confirmed_call captures the listenUrl into call_activity (never exposed elsewhere)",
              call_activity.get(42)["listen_url"] == "wss://vapi.example/listen/xyz")
        check("dial_confirmed_call's outcome string mentions the live-listen link",
              "tok123" in outcome)

        # Failure path: VapiError.
        call_control.clear_all_for_test()
        call_activity.clear(42)
        update_calls.clear()

        async def fake_create_call_fails(*a, **kw):
            raise VapiError("simulated Vapi outage")

        vapi.create_call = fake_create_call_fails
        outcome_fail = await call_tools.dial_confirmed_call(dict(call_row, call_id="c-2"))
        check("a VapiError marks the row failed", any(f.get("status") == "failed" for _, f in update_calls))
        check("a VapiError never registers the call with call_control", call_control.is_active(42) is False)
        check("a VapiError's outcome string surfaces the failure", "couldn't place the call" in outcome_fail.lower())

        # Concurrency cap reached between confirm and dial -- refuses to
        # dial at all (defense in depth).
        call_control.active_count = lambda uid: config.CALL_MAX_CONCURRENT_PER_USER
        update_calls.clear()
        dial_attempted = []

        async def fake_create_call_should_not_run(*a, **kw):
            dial_attempted.append(True)
            return {"id": "should-not-happen"}

        vapi.create_call = fake_create_call_should_not_run
        outcome_cap = await call_tools.dial_confirmed_call(dict(call_row, call_id="c-3"))
        check("concurrency cap reached at dial time: vapi.create_call is never called", dial_attempted == [])
        check("concurrency cap reached at dial time: the row is marked failed",
              any(f.get("status") == "failed" for _, f in update_calls))
    finally:
        vapi.create_call = real_create_call
        call_control.active_count = real_active_count
        db.update_call_session = real_update
        db.get_or_create_live_share_token = real_get_token
        call_control.clear_all_for_test()
        call_activity.clear(42)


# ---------------------------------------------------------------------------
# Part 4: registry.py wiring (structural check)
# ---------------------------------------------------------------------------

def part4_registry_wiring():
    registry_src = (REPO_ROOT / "messa" / "agents" / "registry.py").read_text()
    check("registry.py imports call_tools", "from ..tools import call_tools" in registry_src)
    check("registry.py imports build_call_subagent", "build_call_subagent" in registry_src)
    check("registry.py registers call_agent in the subagents list",
          "build_call_subagent(user," in registry_src)
    check("registry.py's confirm_pending_action tool has a 'place_call' branch",
          '"place_call"' in registry_src and "dial_confirmed_call" in registry_src)


async def main() -> None:
    await part1_gating()
    await part2_propose_call()
    await part3_dial_confirmed_call()
    part4_registry_wiring()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
