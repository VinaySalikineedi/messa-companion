"""Tests for PR3 of the "smart progressive tapbacks + double-text handling"
round (plans/glowing-forging-pumpkin.md, Feature B2b): read-only in-flight-
turn awareness for a follow-up message that arrives well into an already-
running turn.

Four parts:
  1. messa/turn_control.py -- start_turn/end_turn/is_active/active_count/
     describe, token-scoping (ending one turn never clears a different
     still-in-flight one for the same user), the oldest-turn-first
     describe() ordering, and the staleness prune as a leak safety net.
  2. agents/registry.py's in_flight_str prompt block -- empty when the
     flag is off, empty when nothing is in flight, and a real hint
     (never a "go fix it" instruction) when something is.
  3. server.py's _process_inbound wiring -- turn_control.start_turn is
     called AFTER build_orchestrator (never before, since registry.py
     reads describe() while building THIS SAME turn's prompt) and
     end_turn always fires, including when the turn raises.
  4. config.IN_FLIGHT_TURN_AWARENESS_ENABLED defaults to False, and with
     it off _process_inbound never touches turn_control at all.
"""
import asyncio
import os
import sys
import time
import types

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("COMPOSIO_API_KEY", "comp-test-dummy-not-real")

_fake_composio_exceptions = types.ModuleType("composio.exceptions")


class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from messa import config, server, turn_control  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.channels import sendblue  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**kwargs) -> config.UserContext:
    base = dict(user_id=1, phone_number="+15550000000", name="Jane")
    base.update(kwargs)
    return config.UserContext(**base)


# --- Part 1: turn_control.py itself ---

def part1_turn_control_module():
    uid = 90001
    try:
        check("nothing in flight: is_active is False", turn_control.is_active(uid) is False)
        check("nothing in flight: active_count is 0", turn_control.active_count(uid) == 0)
        check("nothing in flight: describe is None", turn_control.describe(uid) is None)

        token1 = turn_control.start_turn(uid, "book me a haircut friday")
        check("start_turn returns a token", token1 is not None)
        check("one turn in flight: is_active True", turn_control.is_active(uid) is True)
        check("one turn in flight: active_count is 1", turn_control.active_count(uid) == 1)
        desc1 = turn_control.describe(uid)
        check("describe mentions the preview text", desc1 is not None and "haircut" in desc1)
        check("describe includes a human recency phrase ('just now' immediately after starting)",
              desc1 is not None and "just now" in desc1)

        # A second, independent turn for the SAME user (double-texting is
        # exactly the scenario this needs to support).
        token2 = turn_control.start_turn(uid, "second unrelated task")
        check("two turns in flight: active_count is 2", turn_control.active_count(uid) == 2)
        desc2 = turn_control.describe(uid)
        check("describe still returns the OLDEST turn's preview, not the newest",
              desc2 is not None and "haircut" in desc2)

        # Ending the FIRST (oldest) turn must not disturb the second.
        turn_control.end_turn(uid, token1)
        check("ending the oldest turn leaves the other one active", turn_control.active_count(uid) == 1)
        desc3 = turn_control.describe(uid)
        check("describe now reflects the remaining turn", desc3 is not None and "second unrelated" in desc3)

        turn_control.end_turn(uid, token2)
        check("ending the last turn clears is_active", turn_control.is_active(uid) is False)
        check("ending the last turn clears describe back to None", turn_control.describe(uid) is None)

        # Ending an already-gone/unknown token is a safe no-op.
        turn_control.end_turn(uid, token1)
        check("ending an already-removed token never raises and stays a no-op",
              turn_control.active_count(uid) == 0)

        # Preview truncation.
        long_preview = "x" * 500
        token3 = turn_control.start_turn(uid, long_preview)
        desc4 = turn_control.describe(uid)
        check("a very long preview is truncated in the description",
              desc4 is not None and len(desc4) < len(long_preview))
        turn_control.end_turn(uid, token3)

        # Staleness prune -- an entry older than _STALE_AFTER_SECONDS is
        # dropped on the next read, as a leak safety net for a killed
        # process that never reached its own end_turn() call.
        token4 = turn_control.start_turn(uid, "stale one")
        turn_control._in_flight[uid][token4].started_at = time.monotonic() - (turn_control._STALE_AFTER_SECONDS + 5)
        check("a stale entry is pruned away on the next read", turn_control.is_active(uid) is False)
    finally:
        turn_control._in_flight.pop(uid, None)


# --- Part 2: agents/registry.py's in_flight_str prompt block ---

def part2_registry_in_flight_prompt_block():
    uid = 90002
    real_flag = config.IN_FLIGHT_TURN_AWARENESS_ENABLED
    try:
        turn_control._in_flight.pop(uid, None)

        # Flag off entirely -- never even looks at turn_control, no matter
        # what's actually in flight.
        config.IN_FLIGHT_TURN_AWARENESS_ENABLED = False
        token = turn_control.start_turn(uid, "a task that's still running")
        u = _user(user_id=uid)
        prompt_off = registry._build_system_prompt(u)
        check("flag off: no in-flight mention in the prompt at all, even with a real turn running",
              "still being processed" not in prompt_off)
        turn_control.end_turn(uid, token)

        # Flag on, nothing in flight -- still no mention.
        config.IN_FLIGHT_TURN_AWARENESS_ENABLED = True
        prompt_nothing = registry._build_system_prompt(u)
        check("flag on, nothing in flight: no in-flight mention", "still being processed" not in prompt_nothing)

        # Flag on, something in flight -- a real hint appears, and it
        # never claims the model can reach in and fix/cancel it.
        token2 = turn_control.start_turn(uid, "book me a haircut friday")
        prompt_active = registry._build_system_prompt(u)
        check("flag on + in flight: mentions another message is still being processed",
              "still being processed" in prompt_active)
        check("flag on + in flight: includes the actual preview text",
              "haircut" in prompt_active)
        check("flag on + in flight: explicitly says it CANNOT reach in and change that turn",
              "cannot reach into" in prompt_active.lower())
        turn_control.end_turn(uid, token2)
    finally:
        config.IN_FLIGHT_TURN_AWARENESS_ENABLED = real_flag
        turn_control._in_flight.pop(uid, None)


# --- Part 3 + 4: server.py's _process_inbound wiring ---

async def part3_process_inbound_wiring():
    from messa import waitlist as waitlist_mod

    real_admission = waitlist_mod.check_new_user_admission
    real_send_typing = sendblue.send_typing_indicator
    real_mark_read = sendblue.mark_read
    real_send_reaction = sendblue.send_reaction
    real_send_message = sendblue.send_message
    real_load_context = server.cli.load_user_context
    real_run_message = server.cli.run_message
    real_build_orchestrator = server.build_orchestrator
    real_react_to_inbound = server._react_to_inbound
    real_flag = config.IN_FLIGHT_TURN_AWARENESS_ENABLED

    uid = 90003
    observed = {"described_during_build": "unset", "described_during_run": "unset"}

    async def fake_admission(number):
        return waitlist_mod.AdmissionDecision(allowed=True)

    async def fake_send_typing(number):
        return {}

    async def fake_mark_read(number):
        return {}

    async def fake_send_reaction(number, message_handle, reaction):
        return {}

    async def fake_send_message(number, content, **kw):
        return {}

    async def fake_load_context(number, name=None, channel="sms", message_handle=None):
        return config.UserContext(user_id=uid, phone_number=number, channel=channel, message_handle=message_handle)

    async def fake_build_orchestrator(user, gate):
        # Captures whether THIS turn can see itself as already in flight --
        # it must not (start_turn happens AFTER this returns).
        observed["described_during_build"] = turn_control.describe(user.user_id)
        return object()

    async def fake_react_to_inbound(number, message_handle, text):
        return None

    waitlist_mod.check_new_user_admission = fake_admission
    sendblue.send_typing_indicator = fake_send_typing
    sendblue.mark_read = fake_mark_read
    sendblue.send_reaction = fake_send_reaction
    sendblue.send_message = fake_send_message
    server.cli.load_user_context = fake_load_context
    server.build_orchestrator = fake_build_orchestrator
    server._react_to_inbound = fake_react_to_inbound
    config.IN_FLIGHT_TURN_AWARENESS_ENABLED = True

    try:
        turn_control._in_flight.pop(uid, None)

        async def fake_run_message_records(user, agent, text, send=None, log_texts=None):
            observed["described_during_run"] = turn_control.describe(user.user_id)
            return "done"

        server.cli.run_message = fake_run_message_records
        await server._process_inbound("+15551234567", "hello there", "sms", message_handle="handle-1")

        check("a turn never sees itself as already in-flight while its own prompt is being built",
              observed["described_during_build"] is None)
        check("turn_control.describe() DOES see the turn as active once run_message is actually running",
              observed["described_during_run"] is not None and "hello there" in observed["described_during_run"])
        check("the turn is cleaned up afterward (no leaked entry)", turn_control.is_active(uid) is False)

        # end_turn must fire even when the turn itself raises.
        async def fake_run_message_raises(user, agent, text, send=None, log_texts=None):
            raise RuntimeError("boom")

        server.cli.run_message = fake_run_message_raises
        await server._process_inbound("+15551234567", "this will blow up", "sms", message_handle="handle-1")
        check("a turn that raises still gets cleaned up from turn_control (finally always runs)",
              turn_control.is_active(uid) is False)

        # Flag OFF: turn_control is never touched at all.
        config.IN_FLIGHT_TURN_AWARENESS_ENABLED = False
        observed["described_during_run"] = "unset"

        async def fake_run_message_flag_off(user, agent, text, send=None, log_texts=None):
            observed["described_during_run"] = turn_control.active_count(user.user_id)
            return "done"

        server.cli.run_message = fake_run_message_flag_off
        await server._process_inbound("+15551234567", "flag is off", "sms", message_handle="handle-1")
        check("flag off: turn_control.active_count stays 0 throughout, never registered",
              observed["described_during_run"] == 0)
    finally:
        waitlist_mod.check_new_user_admission = real_admission
        sendblue.send_typing_indicator = real_send_typing
        sendblue.mark_read = real_mark_read
        sendblue.send_reaction = real_send_reaction
        sendblue.send_message = real_send_message
        server.cli.load_user_context = real_load_context
        server.cli.run_message = real_run_message
        server.build_orchestrator = real_build_orchestrator
        server._react_to_inbound = real_react_to_inbound
        config.IN_FLIGHT_TURN_AWARENESS_ENABLED = real_flag
        turn_control._in_flight.pop(uid, None)


async def main() -> None:
    part1_turn_control_module()
    part2_registry_in_flight_prompt_block()
    await part3_process_inbound_wiring()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
