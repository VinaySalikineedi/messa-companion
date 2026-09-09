"""Tests for the security-critical pieces of PR5 (plans/glowing-forging-pumpkin.md):
messa/tools/call_tools.py's _scrub_sensitive, _build_snapshot,
_is_valid_destination, and the transient assistant's untrusted-input
framing. This is the module that answers the product owner's explicit
"make sure no one can take advantage of this to scam the user, get info,
or make it do things" -- these tests are the closest thing this repo has
to verifying that promise mechanically rather than just by reading the
prompt.

Five parts:
  1. _scrub_sensitive -- card/CVV/SSN key names and value shapes are
     dropped; a plain account/ticket number passes through unmodified
     (per the explicit product decision: the policy target is payment-
     card/CVV/SSN specifically, not "anything that looks sensitive").
  2. _build_snapshot -- only the scrubbed keys survive into
     allowed_info_fields; task_description is always included in the
     snapshot itself.
  3. _is_valid_destination -- E.164 only, emergency shorthands rejected,
     human-formatted numbers rejected.
  4. _build_transient_assistant -- the untrusted-input framing phrases are
     actually present in the system prompt sent to Vapi (same "assert
     phrases appear in the prompt string" style as
     test_persona_prompt_and_guardrail.py), the info tool is scoped to
     exactly the allowed fields (a closed enum, not free text), and no
     info tool is attached at all when there are no allowed fields.
  5. handle_mid_call_tool_call -- the actual mid-call dispatch: an unknown
     provider_call_id is rejected outright; a field not literally in
     allowed_info_fields is rejected (no fuzzy/case-insensitive
     matching); a field that IS allowed is served; the per-call rate
     limit kicks in after MAX_INFO_TOOL_CALLS_PER_CALL calls.
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

from messa import db  # noqa: E402
from messa.tools import call_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Part 1: _scrub_sensitive
# ---------------------------------------------------------------------------

def part1_scrub_sensitive():
    raw = {
        "card_number": "4111111111111111",
        "cvv": "123",
        "cvc": "456",
        "ssn": "123-45-6789",
        "social_security_number": "123456789",
        "usual_order": "grande oat milk latte",
        "account_number": "AB1234567",  # plain account number -- NOT sensitive per policy
        "ticket_number": "T-98765",
        "note_with_embedded_card": "call back re card 4111 1111 1111 1111",  # no spaces version below
        "note_with_embedded_card_nospace": "reference 4111111111111111 on file",
        "note_with_ssn_dashed": "ssn on file: 123-45-6789",
    }
    scrubbed = call_tools._scrub_sensitive(raw)

    check("card_number key is dropped", "card_number" not in scrubbed)
    check("cvv key is dropped", "cvv" not in scrubbed)
    check("cvc key is dropped", "cvc" not in scrubbed)
    check("ssn key is dropped", "ssn" not in scrubbed)
    check("social_security_number key is dropped", "social_security_number" not in scrubbed)
    check("a plain business-context field (usual_order) survives",
          scrubbed.get("usual_order") == "grande oat milk latte")
    check("a plain account_number survives (policy target is card/CVV/SSN specifically)",
          scrubbed.get("account_number") == "AB1234567")
    check("a plain ticket_number survives", scrubbed.get("ticket_number") == "T-98765")
    check("a value containing an unspaced 16-digit card number is dropped by VALUE shape",
          "note_with_embedded_card_nospace" not in scrubbed)
    check("a value containing a dashed SSN is dropped by VALUE shape",
          "note_with_ssn_dashed" not in scrubbed)

    # Case-insensitivity on key names.
    scrubbed2 = call_tools._scrub_sensitive({"CVV": "999", "Card-Number": "4111111111111111"})
    check("key blocklist is case-insensitive (CVV)", "CVV" not in scrubbed2)
    check("key blocklist is case-insensitive (Card-Number)", "Card-Number" not in scrubbed2)

    # Empty/None input never raises.
    check("_scrub_sensitive(None) does not raise and returns {}", call_tools._scrub_sensitive(None) == {})
    check("_scrub_sensitive({}) returns {}", call_tools._scrub_sensitive({}) == {})


# ---------------------------------------------------------------------------
# Part 2: _build_snapshot
# ---------------------------------------------------------------------------

def part2_build_snapshot():
    snapshot_json, allowed_fields = call_tools._build_snapshot(
        "order my usual",
        {"usual_order": "grande oat milk latte", "cvv": "999", "loyalty_number": "L-4412"},
    )
    snapshot = json.loads(snapshot_json)
    check("snapshot always includes task_description", snapshot.get("task_description") == "order my usual")
    check("snapshot includes a scrubbed-safe field", snapshot.get("usual_order") == "grande oat milk latte")
    check("snapshot does NOT include a sensitive field", "cvv" not in snapshot)
    check("allowed_info_fields does not include the sensitive field", "cvv" not in allowed_fields)
    check("allowed_info_fields includes the safe fields",
          set(allowed_fields) == {"usual_order", "loyalty_number"})

    # No info_fields at all -- still works, empty allowlist.
    snapshot_json2, allowed_fields2 = call_tools._build_snapshot("resolve my ticket", None)
    check("no info_fields given: snapshot still has task_description",
          json.loads(snapshot_json2).get("task_description") == "resolve my ticket")
    check("no info_fields given: allowed_fields is empty", allowed_fields2 == [])


# ---------------------------------------------------------------------------
# Part 3: _is_valid_destination
# ---------------------------------------------------------------------------

def part3_is_valid_destination():
    check("a real E.164 US number is valid", call_tools._is_valid_destination("+15551234567") is True)
    check("a real E.164 international number is valid", call_tools._is_valid_destination("+447911123456") is True)
    check("'911' alone is rejected", call_tools._is_valid_destination("911") is False)
    check("'+911' is rejected (still an emergency shorthand)", call_tools._is_valid_destination("+911") is False)
    check("'112' alone is rejected", call_tools._is_valid_destination("112") is False)
    check("a human-formatted number with dashes is rejected",
          call_tools._is_valid_destination("+1-555-123-4567") is False)
    check("a human-formatted number with spaces is rejected",
          call_tools._is_valid_destination("+1 555 123 4567") is False)
    check("missing '+' is rejected", call_tools._is_valid_destination("15551234567") is False)
    check("empty string is rejected", call_tools._is_valid_destination("") is False)
    check("None is rejected", call_tools._is_valid_destination(None) is False)


# ---------------------------------------------------------------------------
# Part 4: _build_transient_assistant
# ---------------------------------------------------------------------------

def part4_build_transient_assistant():
    assistant = call_tools._build_transient_assistant(
        "order a latte", "Starbucks", ["usual_order"],
    )
    system_prompt = assistant["model"]["messages"][0]["content"]

    check("system prompt frames the callee's speech as untrusted",
          "untrusted" in system_prompt.lower())
    check("system prompt explicitly says it is never an instruction",
          "never an instruction" in system_prompt.lower() or "not an instruction" in system_prompt.lower())
    check("system prompt tells the model to decline requests to reveal unscoped info",
          "decline" in system_prompt.lower())
    check("system prompt explicitly forbids reading back/accepting card/CVV/SSN",
          "cvv" in system_prompt.lower() and "social security" in system_prompt.lower())
    check("system prompt names the actual task", "order a latte" in system_prompt)
    check("system prompt warns against 'ignore your instructions'-style injection",
          "ignore" in system_prompt.lower())

    tools = assistant["model"].get("tools", [])
    check("exactly one tool (the info tool) is attached when fields are allowed", len(tools) == 1)
    info_tool = tools[0]["function"]
    check("the info tool is a closed enum, not free text",
          info_tool["parameters"]["properties"]["field"]["enum"] == ["usual_order"])

    # No allowed fields at all -- no info tool attached (nothing for it to serve).
    assistant_no_fields = call_tools._build_transient_assistant("resolve my ticket", None, [])
    check("no info tool attached when there are no allowed fields",
          "tools" not in assistant_no_fields["model"])


# ---------------------------------------------------------------------------
# Part 5: handle_mid_call_tool_call dispatch
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


def _tool_call_payload(provider_call_id, field):
    return {
        "message": {
            "type": "tool-calls",
            "call": {"id": provider_call_id},
            "toolCallId": "tc-1",
            "toolCalls": [{"id": "tc-1", "function": {"name": "get_call_info", "arguments": {"field": field}}}],
        },
    }


async def part5_handle_mid_call_tool_call():
    call_tools._info_tool_call_counts.clear()

    call_row = FakeRow(
        call_id="c1", user_id=7, provider_call_id="vapi-call-1",
        allowed_info_fields=json.dumps(["usual_order"]),
        scratchpad_snapshot=json.dumps({"task_description": "order a latte", "usual_order": "grande oat milk latte"}),
    )

    # Unknown provider_call_id -- rejected outright, no fetchrow beyond the lookup.
    conn = FakeConn(fetchrow_queue=[None])
    install_fake_pool(conn)
    result_unknown = await call_tools.handle_mid_call_tool_call(_tool_call_payload("not-ours", "usual_order"))
    check("unknown provider_call_id is rejected (generic message, not an error)",
          "isn't available" in result_unknown["results"][0]["result"].lower())

    # Known call, field IS in the allowlist -- served.
    conn = FakeConn(fetchrow_queue=[call_row])
    install_fake_pool(conn)
    result_ok = await call_tools.handle_mid_call_tool_call(_tool_call_payload("vapi-call-1", "usual_order"))
    check("an allowed field is served with its real value",
          result_ok["results"][0]["result"] == "grande oat milk latte")

    # Known call, field is NOT in the allowlist -- rejected (no fuzzy match).
    call_tools._info_tool_call_counts.clear()
    conn = FakeConn(fetchrow_queue=[call_row])
    install_fake_pool(conn)
    result_denied = await call_tools.handle_mid_call_tool_call(_tool_call_payload("vapi-call-1", "loyalty_number"))
    check("a field not in the allowlist is rejected", "isn't available" in result_denied["results"][0]["result"].lower())

    # Case-mismatched field name -- rejected too (no case-insensitive aliasing).
    call_tools._info_tool_call_counts.clear()
    conn = FakeConn(fetchrow_queue=[call_row])
    install_fake_pool(conn)
    result_case = await call_tools.handle_mid_call_tool_call(_tool_call_payload("vapi-call-1", "Usual_Order"))
    check("a case-mismatched field name is rejected (no fuzzy/case-insensitive matching)",
          "isn't available" in result_case["results"][0]["result"].lower())

    # Rate limit: after MAX_INFO_TOOL_CALLS_PER_CALL calls, further calls are denied
    # even for an allowed field.
    call_tools._info_tool_call_counts.clear()
    conn = FakeConn(fetchrow_queue=[call_row] * (call_tools.MAX_INFO_TOOL_CALLS_PER_CALL + 1))
    install_fake_pool(conn)
    last_result = None
    for _ in range(call_tools.MAX_INFO_TOOL_CALLS_PER_CALL + 1):
        last_result = await call_tools.handle_mid_call_tool_call(_tool_call_payload("vapi-call-1", "usual_order"))
    check("the call past the per-call rate limit is denied even for an allowed field",
          "isn't available" in last_result["results"][0]["result"].lower())

    call_tools._info_tool_call_counts.clear()


async def main() -> None:
    part1_scrub_sensitive()
    part2_build_snapshot()
    part3_is_valid_destination()
    part4_build_transient_assistant()
    await part5_handle_mid_call_tool_call()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
