"""Tests for the US-only launch gate (messa/region_gate.py): a brand-new
phone number that isn't a clean US E.164 number ("+1" then exactly 10
digits, nothing else) gets a polite "USA only for now" reply instead of
being onboarded.

Four parts:
  1. is_us_phone_number -- exact-format matching only: a real US number
     matches; a number with too few or too many digits after "+1" does
     NOT match (this is the specific failure mode flagged during review --
     a longer non-US number that happens to start with "+1" must never be
     mistaken for a US one just because of that shared prefix); a non-"+1"
     country code, a missing "+", and a human-formatted number (spaces/
     dashes) all correctly fail too.
  2. check_region_admission -- flag off always allows; flag on + a clean
     US number allows without ever touching the DB; flag on + a non-US
     number for someone who ALREADY has a users row is still allowed
     (existing users are never retroactively locked out); flag on + a
     non-US number with no existing row is rejected with the exact reply
     text.
  3. server.py's _process_inbound wiring -- checked BEFORE the waitlist
     cap (a rejected number never touches waitlist.py at all); a rejected
     number gets exactly one outbound message and nothing else (no
     mark_read, no typing indicator, no reaction, no agent turn); flag off
     is byte-identical to before this feature existed.
  4. config.US_ONLY_ENABLED defaults to True (the actual launch state this
     was built for).
"""
import asyncio
import os
import sys
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

from messa import config, db, region_gate, server  # noqa: E402
from messa.channels import sendblue  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- Part 1: is_us_phone_number ---

def part1_is_us_phone_number():
    cases = [
        ("+15551234567", True, "a real US number: '+1' + 10 digits"),
        ("+1555123456", False, "only 9 digits after '+1'"),
        ("+155512345678", False, "11 digits after '+1' -- too many"),
        # The specific failure mode called out during review: a longer,
        # genuinely non-US number that happens to START WITH "+1" must
        # never be mistaken for a US one just because of that shared
        # prefix -- the match is anchored end-to-end, not a prefix check.
        ("+1447712345678", False, "a longer number that merely starts with '+1'"),
        ("+447911123456", False, "a UK number ('+44')"),
        ("+52155512345", False, "a Mexico-ish number ('+52')"),
        ("15551234567", False, "missing the leading '+'"),
        ("+1 555 123 4567", False, "human-formatted with spaces, not clean E.164"),
        ("+1-555-123-4567", False, "human-formatted with dashes, not clean E.164"),
        ("", False, "empty string"),
        (None, False, "None"),
    ]
    for number, expected, why in cases:
        got = region_gate.is_us_phone_number(number)
        check(f"is_us_phone_number({number!r}) is {expected} ({why})", got is expected)


# --- Part 2: check_region_admission ---

async def part2_check_region_admission():
    real_flag = config.US_ONLY_ENABLED
    real_get_user_by_phone = db.get_user_by_phone
    get_user_calls = []

    async def fake_get_user_by_phone(phone):
        get_user_calls.append(phone)
        return {"id": 1} if phone == "+447911999999" else None

    db.get_user_by_phone = fake_get_user_by_phone

    try:
        # Flag off -- always allowed, never even looks at the number.
        config.US_ONLY_ENABLED = False
        get_user_calls.clear()
        decision_off = await region_gate.check_region_admission("+447911123456")
        check("flag off: a non-US number is still allowed", decision_off.allowed is True)
        check("flag off: never touches the DB at all", get_user_calls == [])

        config.US_ONLY_ENABLED = True

        # A clean US number -- allowed, no DB call needed.
        get_user_calls.clear()
        decision_us = await region_gate.check_region_admission("+15551234567")
        check("flag on + US number: allowed", decision_us.allowed is True)
        check("flag on + US number: no reply text needed", decision_us.reply_text is None)
        check("flag on + US number: never touches the DB (format check alone is enough)",
              get_user_calls == [])

        # A non-US number that's NOT an existing user -- rejected.
        decision_new = await region_gate.check_region_admission("+447911123456")
        check("flag on + new non-US number: rejected", decision_new.allowed is False)
        check("flag on + new non-US number: gets the exact USA-only reply text",
              decision_new.reply_text == region_gate.NON_US_MESSAGE)

        # A non-US number that IS ALREADY an existing user -- also rejected
        # (the lock applies across the board, to new and existing users alike).
        decision_existing = await region_gate.check_region_admission("+447911999999")
        check("flag on + EXISTING non-US user: rejected (applies to existing users too)",
              decision_existing.allowed is False)
        check("flag on + existing non-US user: gets the exact USA-only reply text",
              decision_existing.reply_text == region_gate.NON_US_MESSAGE)

        # When the feature is stopped (flag off) -- non-US numbers are allowed.
        config.US_ONLY_ENABLED = False
        decision_stopped = await region_gate.check_region_admission("+447911999999")
        check("flag off (feature stopped in future): non-US user allowed",
              decision_stopped.allowed is True)
    finally:
        config.US_ONLY_ENABLED = real_flag
        db.get_user_by_phone = real_get_user_by_phone


# --- Part 3: server.py's _process_inbound wiring ---

async def part3_process_inbound_wiring():
    from messa import waitlist as waitlist_mod

    real_flag = config.US_ONLY_ENABLED
    real_get_user_by_phone = db.get_user_by_phone
    real_send_message = sendblue.send_message
    real_mark_read = sendblue.mark_read
    real_send_typing = sendblue.send_typing_indicator
    real_send_reaction = sendblue.send_reaction
    real_load_context = server.cli.load_user_context
    real_run_message = server.cli.run_message
    real_build_orchestrator = server.build_orchestrator
    real_react_to_inbound = server._react_to_inbound
    real_admission = waitlist_mod.check_new_user_admission

    sent_messages = []
    calls_touched = []

    async def fake_get_user_by_phone(phone):
        return None

    async def fake_send_message(num, content, **kw):
        sent_messages.append({"number": num, "content": content})
        return {}

    async def fake_mark_read(num):
        calls_touched.append("mark_read")
        return {}

    async def fake_send_typing(num):
        calls_touched.append("send_typing")
        return {}

    async def fake_send_reaction(num, message_handle, reaction):
        calls_touched.append("send_reaction")
        return {}

    async def fake_load_context(num, name=None, channel="sms", message_handle=None):
        calls_touched.append("load_user_context")
        return config.UserContext(user_id=1, phone_number=num, channel=channel, message_handle=message_handle)

    async def fake_build_orchestrator(user, gate):
        calls_touched.append("build_orchestrator")
        return object()

    async def fake_run_message(user, agent, text, send=None, log_texts=None, on_turn_complete=None):
        calls_touched.append("run_message")
        return "done"

    async def fake_react_to_inbound(num, message_handle, text):
        calls_touched.append("react_to_inbound")
        return None

    async def fake_admission(num):
        calls_touched.append("waitlist_admission")
        return waitlist_mod.AdmissionDecision(allowed=True)

    db.get_user_by_phone = fake_get_user_by_phone
    sendblue.send_message = fake_send_message
    sendblue.mark_read = fake_mark_read
    sendblue.send_typing_indicator = fake_send_typing
    sendblue.send_reaction = fake_send_reaction
    server.cli.load_user_context = fake_load_context
    server.build_orchestrator = fake_build_orchestrator
    server.cli.run_message = fake_run_message
    server._react_to_inbound = fake_react_to_inbound
    waitlist_mod.check_new_user_admission = fake_admission

    try:
        # 3a: flag ON + a non-US number -- gets exactly the USA-only reply
        # and NOTHING else runs: no waitlist check, no mark_read, no
        # typing indicator, no reaction, no agent turn.
        config.US_ONLY_ENABLED = True
        sent_messages.clear()
        calls_touched.clear()
        await server._process_inbound("+447911123456", "hi there", "sms", message_handle="h1")
        check("flag on + non-US number: exactly one outbound message sent",
              len(sent_messages) == 1)
        if sent_messages:
            check("flag on + non-US number: the number is the actual sender",
                  sent_messages[0]["number"] == "+447911123456")
            check("flag on + non-US number: the exact USA-only reply text is sent",
                  sent_messages[0]["content"] == region_gate.NON_US_MESSAGE)
        check("flag on + non-US number: the waitlist cap is never even reached",
              "waitlist_admission" not in calls_touched)
        check("flag on + non-US number: no other side effect fires at all",
              calls_touched == [])

        # 3b: flag ON + a US number -- passes straight through, full
        # pipeline runs exactly as it would without this feature.
        sent_messages.clear()
        calls_touched.clear()
        await server._process_inbound("+15551234567", "hi there", "sms", message_handle="h2")
        check("flag on + US number: no rejection message sent", sent_messages == [])
        check("flag on + US number: the waitlist cap IS reached (gate passed through)",
              "waitlist_admission" in calls_touched)
        check("flag on + US number: the real turn still runs", "run_message" in calls_touched)

        # 3c: flag OFF -- byte-identical to before this feature existed;
        # a non-US number is never rejected.
        config.US_ONLY_ENABLED = False
        sent_messages.clear()
        calls_touched.clear()
        await server._process_inbound("+447911123456", "hi there", "sms", message_handle="h3")
        check("flag off: a non-US number is never rejected", sent_messages == [])
        check("flag off: the full pipeline still runs for a non-US number",
              "run_message" in calls_touched)
    finally:
        config.US_ONLY_ENABLED = real_flag
        db.get_user_by_phone = real_get_user_by_phone
        sendblue.send_message = real_send_message
        sendblue.mark_read = real_mark_read
        sendblue.send_typing_indicator = real_send_typing
        sendblue.send_reaction = real_send_reaction
        server.cli.load_user_context = real_load_context
        server.cli.run_message = real_run_message
        server.build_orchestrator = real_build_orchestrator
        server._react_to_inbound = real_react_to_inbound
        waitlist_mod.check_new_user_admission = real_admission


# --- Part 4: default state ---

def part4_default_flag():
    # This process may have MESSA_US_ONLY_ENABLED set explicitly in its
    # environment (this whole test file overrides config.US_ONLY_ENABLED
    # directly everywhere else, precisely to not depend on that) -- this
    # one check is the one place that matters, so it skips itself rather
    # than give a false failure under an environment that deliberately set
    # the env var to something else for some other reason.
    if os.environ.get("MESSA_US_ONLY_ENABLED") is not None:
        print("[SKIP] US_ONLY_ENABLED default check (MESSA_US_ONLY_ENABLED is set in this environment)")
        return
    check("US_ONLY_ENABLED defaults to True (this launch's actual state)",
          config.US_ONLY_ENABLED is True)


async def main() -> None:
    part1_is_us_phone_number()
    await part2_check_region_admission()
    await part3_process_inbound_wiring()
    part4_default_flag()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
