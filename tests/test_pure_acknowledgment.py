"""Tests for PR5 of the "smart progressive tapbacks + double-text handling"
round (plans/glowing-forging-pumpkin.md, Feature B2a): the pure-
acknowledgment short-circuit -- a bare "thanks"/"ok"/"👍" sent after
Messa's already replied gets a deterministic tapback and no agent turn,
instead of costing a full orchestrator turn for a reply nobody needed.

Four parts:
  1. server.py's _pure_acknowledgment_reaction -- exact-match only (never
     fuzzy/substring), case/whitespace/trailing-punctuation insensitive,
     gratitude vs neutral vocabulary map to different deterministic
     reactions, and anything not in either closed vocabulary returns None.
  2. server.py's _handle_pure_acknowledgment -- the four guards (unknown
     phone number, onboarding incomplete, no prior assistant message, last
     assistant message ended in "?") each fall through to "not handled"
     (False) rather than short-circuiting; a genuine match logs the raw
     text, logs a system marker, sends the reaction, and returns True.
  3. server.py's _process_inbound wiring -- config.PURE_ACKNOWLEDGMENT_
     SHORTCIRCUIT_ENABLED defaults to False (byte-identical to before this
     PR with it off); flag on + iMessage + a genuine match skips the real
     turn entirely; flag on but no message_handle (SMS/RCS) always runs
     the full pipeline regardless of the text.
  4. A non-acknowledgment message (or an ack that fails a guard) still
     runs the complete, unmodified pipeline.
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

from messa import config, db, server  # noqa: E402
from messa.channels import sendblue  # noqa: E402
from messa.channels.sendblue import SendblueError  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- Part 1: _pure_acknowledgment_reaction ---

def part1_pure_acknowledgment_reaction():
    for text in ["thanks", "Thanks", "THANKS!", "  thank you  ", "ty", "tysm", "🙏", "❤️"]:
        check(f"gratitude match: {text!r}", server._pure_acknowledgment_reaction(text) == server._ACK_REACTION_GRATITUDE)

    for text in ["ok", "Okay!", "  k  ", "cool", "great", "perfect", "got it", "👍", "sounds good."]:
        check(f"neutral match: {text!r}", server._pure_acknowledgment_reaction(text) == server._ACK_REACTION_NEUTRAL)

    for text in [
        "thanks, also book the hotel",  # gratitude PLUS a real request -- must not match
        "ok can you also text sarah",
        "",
        "   ",
        "what time is it",
        "perfect, but can you check one more thing",
        "thank",  # not an exact vocabulary entry
    ]:
        check(f"non-match falls through to None: {text!r}", server._pure_acknowledgment_reaction(text) is None)


# --- Part 2: _handle_pure_acknowledgment's guards ---

async def part2_handle_pure_acknowledgment_guards():
    real_get_user_by_phone = db.get_user_by_phone
    real_get_recent_messages = db.get_recent_messages
    real_append_message = db.append_message
    real_mark_read = sendblue.mark_read
    real_send_reaction = sendblue.send_reaction

    appended = []
    reactions_sent = []

    async def fake_append_message(user_id, role, content, channel="cli"):
        appended.append((user_id, role, content))
        return len(appended)

    async def fake_mark_read(number):
        return {}

    async def fake_send_reaction(number, message_handle, reaction):
        reactions_sent.append(reaction)
        return {}

    db.append_message = fake_append_message
    sendblue.mark_read = fake_mark_read
    sendblue.send_reaction = fake_send_reaction

    try:
        # 2a: unknown phone number -> False, nothing touched.
        async def fake_get_user_none(phone):
            return None
        db.get_user_by_phone = fake_get_user_none
        appended.clear()
        reactions_sent.clear()
        result = await server._handle_pure_acknowledgment(
            "+15559990000", "thanks", "sms", "handle-1", server._ACK_REACTION_GRATITUDE)
        check("unknown phone number: falls through (returns False)", result is False)
        check("unknown phone number: nothing logged or reacted", appended == [] and reactions_sent == [])

        # 2b: onboarding not complete -> False.
        async def fake_get_user_onboarding(phone):
            return {"id": 42, "onboarding_step": "awaiting_name"}
        db.get_user_by_phone = fake_get_user_onboarding
        appended.clear()
        reactions_sent.clear()
        result2 = await server._handle_pure_acknowledgment(
            "+15551234567", "ok", "sms", "handle-1", server._ACK_REACTION_NEUTRAL)
        check("onboarding incomplete: falls through (returns False)", result2 is False)
        check("onboarding incomplete: nothing logged or reacted", appended == [] and reactions_sent == [])

        # 2c: no assistant message on record at all -> False.
        async def fake_get_user_complete(phone):
            return {"id": 43, "onboarding_step": "complete"}

        async def fake_recent_no_assistant(user_id, limit=20):
            return [{"id": 1, "role": "user", "content": "hi"}]

        db.get_user_by_phone = fake_get_user_complete
        db.get_recent_messages = fake_recent_no_assistant
        appended.clear()
        reactions_sent.clear()
        result3 = await server._handle_pure_acknowledgment(
            "+15551234567", "ok", "sms", "handle-1", server._ACK_REACTION_NEUTRAL)
        check("no assistant message on record: falls through (returns False)", result3 is False)

        # 2d: last assistant message ended in "?" -> False.
        async def fake_recent_pending_question(user_id, limit=20):
            return [
                {"id": 1, "role": "user", "content": "book me a haircut friday?"},
                {"id": 2, "role": "assistant", "content": "Want me to book it for 2pm?"},
            ]
        db.get_recent_messages = fake_recent_pending_question
        appended.clear()
        reactions_sent.clear()
        result4 = await server._handle_pure_acknowledgment(
            "+15551234567", "perfect", "sms", "handle-1", server._ACK_REACTION_NEUTRAL)
        check("a pending yes/no question: falls through (returns False)", result4 is False)
        check("a pending yes/no question: nothing logged or reacted", appended == [] and reactions_sent == [])

        # 2e: a genuine match -- everything fires, in order, returns True.
        async def fake_recent_no_question(user_id, limit=20):
            return [
                {"id": 1, "role": "user", "content": "text sarah I'm late"},
                {"id": 2, "role": "assistant", "content": "Done, texted her."},
            ]
        db.get_recent_messages = fake_recent_no_question
        appended.clear()
        reactions_sent.clear()
        result5 = await server._handle_pure_acknowledgment(
            "+15551234567", "thanks!", "sms", "handle-1", server._ACK_REACTION_GRATITUDE)
        check("genuine match: returns True", result5 is True)
        check("genuine match: the raw text is logged as a user message",
              any(role == "user" and content == "thanks!" for (_uid, role, content) in appended))
        check("genuine match: a system marker is also logged",
              any(role == "system" for (_uid, role, content) in appended))
        check("genuine match: the deterministic reaction is actually sent",
              reactions_sent == [server._ACK_REACTION_GRATITUDE])

        # 2f: a failed Sendblue reaction send is swallowed, still returns True.
        async def failing_send_reaction(number, message_handle, reaction):
            raise SendblueError("boom")
        sendblue.send_reaction = failing_send_reaction
        appended.clear()
        result6 = await server._handle_pure_acknowledgment(
            "+15551234567", "ok", "sms", "handle-1", server._ACK_REACTION_NEUTRAL)
        check("a failed reaction send is swallowed and still returns True (text was still logged)",
              result6 is True)
    finally:
        db.get_user_by_phone = real_get_user_by_phone
        db.get_recent_messages = real_get_recent_messages
        db.append_message = real_append_message
        sendblue.mark_read = real_mark_read
        sendblue.send_reaction = real_send_reaction


# --- Part 3 + 4: _process_inbound wiring ---

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
    real_get_user_by_phone = db.get_user_by_phone
    real_get_recent_messages = db.get_recent_messages
    real_append_message = db.append_message
    real_flag = config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED

    run_message_calls = []
    reactions_sent = []

    async def fake_admission(number):
        return waitlist_mod.AdmissionDecision(allowed=True)

    async def fake_send_typing(number):
        return {}

    async def fake_mark_read(number):
        return {}

    async def fake_send_reaction(number, message_handle, reaction):
        reactions_sent.append(reaction)
        return {}

    async def fake_send_message(number, content, **kw):
        return {}

    async def fake_load_context(number, name=None, channel="sms", message_handle=None):
        return config.UserContext(user_id=1, phone_number=number, channel=channel, message_handle=message_handle)

    async def fake_build_orchestrator(user, gate):
        return object()

    async def fake_run_message(user, agent, text, send=None, log_texts=None, on_turn_complete=None):
        run_message_calls.append(text)
        return "done"

    async def fake_react_to_inbound(number, message_handle, text):
        return None

    async def fake_get_user_complete(phone):
        return {"id": 1, "onboarding_step": "complete"}

    async def fake_recent_no_question(user_id, limit=20):
        return [{"id": 1, "role": "assistant", "content": "Done, texted her."}]

    async def fake_append_message(user_id, role, content, channel="cli"):
        return 1

    waitlist_mod.check_new_user_admission = fake_admission
    sendblue.send_typing_indicator = fake_send_typing
    sendblue.mark_read = fake_mark_read
    sendblue.send_reaction = fake_send_reaction
    sendblue.send_message = fake_send_message
    server.cli.load_user_context = fake_load_context
    server.build_orchestrator = fake_build_orchestrator
    server.cli.run_message = fake_run_message
    server._react_to_inbound = fake_react_to_inbound
    db.get_user_by_phone = fake_get_user_complete
    db.get_recent_messages = fake_recent_no_question
    db.append_message = fake_append_message

    try:
        # 3a: flag OFF (the actual default) -- a bare "thanks" still runs
        # the full pipeline, byte-identical to before this PR.
        check("PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED defaults to True",
              config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED is True)
        config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED = False
        run_message_calls.clear()
        reactions_sent.clear()
        await server._process_inbound("+15551234567", "thanks!", "sms", message_handle="handle-1")
        check("flag off: the full turn still runs even for a bare 'thanks'",
              run_message_calls == ["thanks!"])

        # 3b: flag ON, iMessage, genuine ack -- the real turn is skipped
        # entirely.
        config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED = True
        run_message_calls.clear()
        reactions_sent.clear()
        await server._process_inbound("+15551234567", "thanks!", "sms", message_handle="handle-1")
        check("flag on + iMessage + genuine ack: the real turn never runs",
              run_message_calls == [])
        check("flag on + iMessage + genuine ack: the deterministic reaction is sent",
              reactions_sent == [server._ACK_REACTION_GRATITUDE])

        # 3c: flag ON, but NO message_handle (SMS/RCS) -- always the full
        # pipeline, regardless of the text.
        run_message_calls.clear()
        reactions_sent.clear()
        await server._process_inbound("+15551234567", "thanks!", "sms", message_handle=None)
        check("flag on but no message_handle: the full turn still runs (no tapback support on this channel)",
              run_message_calls == ["thanks!"])

        # 3d: flag ON, iMessage, NOT an acknowledgment -- full pipeline.
        run_message_calls.clear()
        reactions_sent.clear()
        await server._process_inbound("+15551234567", "can you also text sarah", "sms", message_handle="handle-1")
        check("flag on + iMessage + not an acknowledgment: the full turn still runs",
              run_message_calls == ["can you also text sarah"])
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
        db.get_user_by_phone = real_get_user_by_phone
        db.get_recent_messages = real_get_recent_messages
        db.append_message = real_append_message
        config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED = real_flag


async def main() -> None:
    part1_pure_acknowledgment_reaction()
    await part2_handle_pure_acknowledgment_guards()
    await part3_process_inbound_wiring()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
