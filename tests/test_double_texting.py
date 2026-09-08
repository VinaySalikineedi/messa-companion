"""Tests for PR6 of the "smart progressive tapbacks + double-text handling"
round (plans/glowing-forging-pumpkin.md, Feature B1): double-text batching
-- when two texts land seconds apart, the second is folded into the same
turn as a correction/addition rather than run as its own uncoordinated
agent turn.

(Feature B2a's pure-acknowledgment short-circuit already has its own full
test file, tests/test_pure_acknowledgment.py; Feature B2b's turn_control
module and its registry.py prompt block already have theirs,
tests/test_turn_control.py. Part 5e here only adds the one thing those
files can't: proving pure-ack short-circuits BEFORE batching even touches
_pending_batches, which needs both features wired at once.)

Six parts:
  1. server.py's _combine_batched_messages -- a single-message batch is
     returned completely unchanged (no framing at all); a multi-message
     batch gets the exact bracketed-note framing from the plan (timing
     window, "reply once to all of it together", numbered follow-ups).
  2. server.py's _collect_batch_or_follow -- leader/follower mechanics: the
     first call for a number registers the batch and does the actual
     waiting; any call that arrives while a batch is forming appends and
     returns None immediately, without itself calling the typing
     indicator or waiting on anything.
  3. The DOUBLE_TEXT_MAX_COLLECTION_SECONDS ceiling -- a batch that keeps
     growing every debounce window still gets cut off and processed once
     the hard ceiling is reached, so a rapid-fire burst can't defer
     processing indefinitely.
  4. Crash-safety -- an exception during collection (simulated via a
     typing-indicator call that raises something other than
     SendblueError) still drains and returns whatever was collected, and
     always clears the module-level _pending_batches entry so a crash can
     never leave a phantom "still forming" entry stuck for that number.
  5. server.py's _process_inbound wiring -- config.DOUBLE_TEXT_BATCHING_
     ENABLED defaults to False (byte-identical to before this PR with it
     off); flag on + a genuine two-message double-text merges into one
     real turn with log_texts populated and the reaction targeting the
     LAST message's handle; flag on + a lone message (no follower ever
     arrives) still runs, unchanged content, after the debounce window;
     and the pure-acknowledgment short-circuit (when also enabled) fires
     BEFORE batching ever registers a pending entry.
  6. messa/cli.py's run_message(log_texts=...) history-building behavior,
     exercised directly (not just the standalone sanity check from PR4):
     with log_texts given, each raw text is logged as its own row, those
     just-logged rows are excluded from the history read-back, the read
     limit is raised by the batch size, and the combined `text` is what
     actually gets appended to history for the model to see. With
     log_texts=None (every pre-PR6 caller), behavior is byte-for-byte
     identical to before this parameter existed.
"""
import asyncio
import itertools
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

from messa import cli, config, db, server, usage  # noqa: E402
from messa.channels.sendblue import SendblueError  # noqa: E402
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


# --- Part 1: _combine_batched_messages ---

def part1_combine_batched_messages():
    single = [server._PendingInbound(content="text sarah I'm running late", message_handle="h1", received_at=100.0)]
    combined_single = server._combine_batched_messages(single)
    check("a single-message batch is returned completely unchanged",
          combined_single == "text sarah I'm running late")

    batch = [
        server._PendingInbound(content="text sarah that ill be 15 min late", message_handle="h1", received_at=100.0),
        server._PendingInbound(content="*sara", message_handle="h2", received_at=103.4),
    ]
    combined = server._combine_batched_messages(batch)
    check("multi-message combination starts with the first message's content",
          combined.startswith("text sarah that ill be 15 min late"))
    check("multi-message combination reports the correct count and rounded window",
          "sent 2 texts in quick succession (within 3s)" in combined)
    check("multi-message combination explicitly frames a later text as a correction/addition",
          "a later text is usually a correction, a clarification, or an addition" in combined)
    check("multi-message combination asks for one reply covering all of it",
          "Reply once, to all of it together" in combined)
    check("multi-message combination numbers the follow-up with its own delay",
          "[Text 2 of 2, sent 3s after the first:]" in combined)
    check("multi-message combination includes the follow-up's exact text",
          combined.rstrip().endswith("*sara"))

    # Three messages -- confirms the numbering isn't hardcoded to "2 of 2".
    batch3 = [
        server._PendingInbound(content="book me a haircut", message_handle="h1", received_at=0.0),
        server._PendingInbound(content="friday please", message_handle="h2", received_at=1.0),
        server._PendingInbound(content="actually saturday", message_handle="h3", received_at=2.0),
    ]
    combined3 = server._combine_batched_messages(batch3)
    check("a 3-message batch reports the correct total count",
          "sent 3 texts in quick succession" in combined3)
    check("a 3-message batch numbers its second follow-up 'Text 2 of 3'",
          "[Text 2 of 3, sent 1s after the first:]" in combined3)
    check("a 3-message batch numbers its third follow-up 'Text 3 of 3'",
          "[Text 3 of 3, sent 2s after the first:]" in combined3)


# --- Part 2: _collect_batch_or_follow leader/follower mechanics ---

async def part2_leader_follower_mechanics():
    number = "+15559990001"
    real_debounce = config.DOUBLE_TEXT_DEBOUNCE_SECONDS
    real_ceiling = config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS
    real_typing = sendblue.send_typing_indicator
    typing_calls = []

    async def fake_typing(num):
        typing_calls.append(num)
        return {}

    config.DOUBLE_TEXT_DEBOUNCE_SECONDS = 0.05
    config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS = 2.0
    sendblue.send_typing_indicator = fake_typing
    server._pending_batches.pop(number, None)

    try:
        leader_task = asyncio.create_task(server._collect_batch_or_follow(number, "msg1", "h1"))
        await asyncio.sleep(0.01)  # let the leader register itself and enter its debounce wait
        check("the batch is registered under the phone number while the leader is collecting",
              number in server._pending_batches)

        follow_result = await server._collect_batch_or_follow(number, "msg2", "h2")
        check("a follower call returns None immediately (never runs its own turn)",
              follow_result is None)
        check("a follower call never fires its own typing indicator",
              typing_calls == [number])  # exactly the leader's one call, not two

        result = await asyncio.wait_for(leader_task, timeout=2.0)
        check("the leader returns a 3-tuple once collection drains", result is not None and len(result) == 3)
        combined_content, log_texts, last_handle = result
        check("log_texts preserves every raw message, leader first", log_texts == ["msg1", "msg2"])
        check("the combined content folds in both messages", "msg1" in combined_content and "msg2" in combined_content)
        check("the returned handle is the LAST message's handle, not the leader's own",
              last_handle == "h2")
        check("the pending-batch entry is cleared once drained", number not in server._pending_batches)
    finally:
        config.DOUBLE_TEXT_DEBOUNCE_SECONDS = real_debounce
        config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS = real_ceiling
        sendblue.send_typing_indicator = real_typing
        server._pending_batches.pop(number, None)


# --- Part 3: the DOUBLE_TEXT_MAX_COLLECTION_SECONDS ceiling ---

async def part3_collection_ceiling():
    number = "+15559990002"
    real_debounce = config.DOUBLE_TEXT_DEBOUNCE_SECONDS
    real_ceiling = config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS
    real_typing = sendblue.send_typing_indicator

    async def fake_typing(num):
        return {}

    # Debounce is short enough that a follower has to keep arriving faster
    # than it to prevent the "nothing new arrived" break -- the ceiling is
    # what's actually being tested here, not the debounce itself.
    config.DOUBLE_TEXT_DEBOUNCE_SECONDS = 0.03
    config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS = 0.12
    sendblue.send_typing_indicator = fake_typing
    server._pending_batches.pop(number, None)

    appender_stop = False

    async def keep_appending():
        i = 0
        while not appender_stop:
            await asyncio.sleep(0.015)
            i += 1
            # A follower arriving on an already-drained batch would start a
            # brand new leader of its own -- harmless, but not what this
            # test is measuring, so it stops the instant the real leader
            # has drained (appender_stop flips right after that).
            if appender_stop:
                break
            await server._collect_batch_or_follow(number, f"follow-{i}", f"h-follow-{i}")

    try:
        started = asyncio.get_event_loop().time()
        leader_task = asyncio.create_task(server._collect_batch_or_follow(number, "msg1", "h1"))
        appender_task = asyncio.create_task(keep_appending())
        result = await asyncio.wait_for(leader_task, timeout=2.0)
        elapsed = asyncio.get_event_loop().time() - started
        appender_stop = True
        appender_task.cancel()
        try:
            await appender_task
        except asyncio.CancelledError:
            pass

        check("a continuously-growing batch is still cut off near the ceiling, not left to run indefinitely",
              elapsed < config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS + 0.3)
        combined_content, log_texts, last_handle = result
        check("the batch actually grew past the leader's own single message before the ceiling hit",
              len(log_texts) > 1)
        check("the pending-batch entry is cleared even when cut off by the ceiling",
              number not in server._pending_batches)
    finally:
        appender_stop = True
        config.DOUBLE_TEXT_DEBOUNCE_SECONDS = real_debounce
        config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS = real_ceiling
        sendblue.send_typing_indicator = real_typing
        server._pending_batches.pop(number, None)


# --- Part 4: crash-safety ---

async def part4_crash_safety():
    number = "+15559990003"
    real_typing = sendblue.send_typing_indicator

    async def raising_typing(num):
        raise RuntimeError("boom -- not a SendblueError, must escape the inner try")

    sendblue.send_typing_indicator = raising_typing
    server._pending_batches.pop(number, None)

    try:
        # The typing-indicator call raises before the collection loop ever
        # starts, so this must still drain to exactly the leader's own
        # single message -- a crash mid-collection must never just lose
        # messages that already arrived.
        result = await server._collect_batch_or_follow(number, "only message", "h1")
        check("a crash during collection still returns a usable result", result is not None)
        combined_content, log_texts, last_handle = result
        check("a crash during collection still returns whatever was collected so far",
              log_texts == ["only message"])
        check("a single-message result after a crash is still unframed (no follower ever joined)",
              combined_content == "only message")
        check("the handle from before the crash is still returned", last_handle == "h1")
        check("the pending-batch entry is cleared even when collection crashed",
              number not in server._pending_batches)
    finally:
        sendblue.send_typing_indicator = real_typing
        server._pending_batches.pop(number, None)


# --- Part 5: server.py's _process_inbound wiring ---

async def part5_process_inbound_wiring():
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
    real_batching_flag = config.DOUBLE_TEXT_BATCHING_ENABLED
    real_ack_flag = config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED
    real_debounce = config.DOUBLE_TEXT_DEBOUNCE_SECONDS
    real_ceiling = config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS

    number = "+15559990004"
    run_message_calls = []
    reaction_calls = []

    async def fake_admission(num):
        return waitlist_mod.AdmissionDecision(allowed=True)

    async def fake_send_typing(num):
        return {}

    async def fake_mark_read(num):
        return {}

    async def fake_send_reaction(num, message_handle, reaction):
        return {}

    async def fake_send_message(num, content, **kw):
        return {}

    async def fake_load_context(num, name=None, channel="sms", message_handle=None):
        return config.UserContext(user_id=1, phone_number=num, channel=channel, message_handle=message_handle)

    async def fake_build_orchestrator(user, gate):
        return object()

    async def fake_run_message(user, agent, text, send=None, log_texts=None):
        run_message_calls.append({"text": text, "log_texts": log_texts})
        return "done"

    async def fake_react_to_inbound(num, message_handle, text):
        reaction_calls.append({"message_handle": message_handle, "text": text})
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
    config.DOUBLE_TEXT_DEBOUNCE_SECONDS = 0.03
    config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS = 1.0

    try:
        # 5a: flag OFF (the actual default) -- byte-identical to before
        # this PR, no debounce, no batching machinery touched at all.
        check("DOUBLE_TEXT_BATCHING_ENABLED defaults to True",
              config.DOUBLE_TEXT_BATCHING_ENABLED is True)
        config.DOUBLE_TEXT_BATCHING_ENABLED = False
        config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED = False
        run_message_calls.clear()
        reaction_calls.clear()
        await server._process_inbound(number, "one plain message", "sms", message_handle="h-solo")
        check("flag off: exactly one run_message call, with log_texts=None (unchanged today)",
              run_message_calls == [{"text": "one plain message", "log_texts": None}])
        check("flag off: _pending_batches is never touched", number not in server._pending_batches)

        # 5b: flag ON, a genuine two-message double-text -- merges into
        # ONE real turn, log_texts populated, reaction targets the LAST
        # message's handle.
        config.DOUBLE_TEXT_BATCHING_ENABLED = True
        run_message_calls.clear()
        reaction_calls.clear()
        leader_task = asyncio.create_task(
            server._process_inbound(number, "text sarah that ill be 15 min late", "sms", message_handle="h1")
        )
        await asyncio.sleep(0.01)  # let the leader register + enter its debounce wait
        await server._process_inbound(number, "*sara", "sms", message_handle="h2")
        await asyncio.wait_for(leader_task, timeout=2.0)

        check("flag on + double-text: exactly one run_message call (not two)", len(run_message_calls) == 1)
        if run_message_calls:
            call = run_message_calls[0]
            check("flag on + double-text: log_texts carries each raw message",
                  call["log_texts"] == ["text sarah that ill be 15 min late", "*sara"])
            check("flag on + double-text: the combined text folds in both messages",
                  "text sarah that ill be 15 min late" in call["text"] and "*sara" in call["text"])
        check("flag on + double-text: exactly one reaction classification call, not two",
              len(reaction_calls) == 1)
        if reaction_calls:
            check("flag on + double-text: the reaction targets the LAST message's handle",
                  reaction_calls[0]["message_handle"] == "h2")
        check("flag on + double-text: the pending-batch entry is cleared afterward",
              number not in server._pending_batches)

        # 5c: flag ON, but no follower ever arrives -- still runs, content
        # unchanged (no framing for a lone message), after the debounce.
        run_message_calls.clear()
        reaction_calls.clear()
        await server._process_inbound(number, "just one message, no follow-up", "sms", message_handle="h-lonely")
        check("flag on + lone message: still runs, exactly once", len(run_message_calls) == 1)
        if run_message_calls:
            check("flag on + lone message: content is completely unchanged (no bracketed framing)",
                  run_message_calls[0]["text"] == "just one message, no follow-up")
            check("flag on + lone message: log_texts is still populated (just the one message)",
                  run_message_calls[0]["log_texts"] == ["just one message, no follow-up"])

        # 5d: BOTH flags on -- a pure acknowledgment short-circuits before
        # batching ever registers a pending entry (see the ordering
        # comment directly above the batching block in _process_inbound).
        config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED = True
        run_message_calls.clear()
        reaction_calls.clear()
        await server._process_inbound(number, "thanks!", "sms", message_handle="h-ack")
        check("both flags on + a pure ack: the real turn never runs",
              run_message_calls == [])
        check("both flags on + a pure ack: batching never even registered a pending entry for it",
              number not in server._pending_batches)
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
        config.DOUBLE_TEXT_BATCHING_ENABLED = real_batching_flag
        config.PURE_ACKNOWLEDGMENT_SHORTCIRCUIT_ENABLED = real_ack_flag
        config.DOUBLE_TEXT_DEBOUNCE_SECONDS = real_debounce
        config.DOUBLE_TEXT_MAX_COLLECTION_SECONDS = real_ceiling
        server._pending_batches.pop(number, None)


# --- Part 6: cli.run_message(log_texts=...) history-building behavior ---

async def part6_run_message_log_texts():
    real_append_message = db.append_message
    real_get_recent_messages = db.get_recent_messages
    real_run_turn = cli.run_turn
    real_peek_usage = usage.peek_usage
    real_check_and_consume = usage.check_and_consume

    id_counter = itertools.count(100)
    appended = []
    captured_limits = []
    captured_histories = []
    seed_recent: list[dict] = []

    async def fake_append_message(user_id, role, content, channel="cli"):
        row_id = next(id_counter)
        appended.append({"id": row_id, "role": role, "content": content, "channel": channel})
        return row_id

    async def fake_get_recent_messages(user_id, limit=20):
        captured_limits.append(limit)
        return list(seed_recent)

    async def fake_run_turn(agent, history, on_ai_message=None, _allow_retry=True):
        captured_histories.append(list(history))
        if on_ai_message:
            await on_ai_message("ok, done")
        return []

    async def fake_peek_usage(user, feature):
        raise AssertionError("usage.peek_usage should never be reached for an is_admin user")

    async def fake_check_and_consume(user, feature):
        raise AssertionError("usage.check_and_consume should never be reached for an is_admin user")

    db.append_message = fake_append_message
    db.get_recent_messages = fake_get_recent_messages
    cli.run_turn = fake_run_turn
    usage.peek_usage = fake_peek_usage
    usage.check_and_consume = fake_check_and_consume

    # is_admin=True skips both usage gates entirely (see UserContext.
    # has_deepsearch_access's own docstring for the same is_admin
    # shortcut elsewhere) -- onboarding_step="complete" means
    # _onboarding_complete_messages is a guaranteed no-op read, so this
    # test isolates exactly the history-building logic PR4 added, nothing
    # else in run_message's much larger body.
    user = _user(user_id=42, is_admin=True, onboarding_step="complete")

    try:
        # 6a: log_texts=None -- byte-identical to every pre-PR6 caller.
        appended.clear()
        captured_limits.clear()
        captured_histories.clear()
        seed_recent[:] = [{"id": 1, "role": "assistant", "content": "earlier reply"}]
        reply = await cli.run_message(user, agent=object(), text="hello", send=None, log_texts=None)
        check("log_texts=None: exactly one user row logged, the raw text itself",
              [a for a in appended if a["role"] == "user"] == [{"id": appended[0]["id"], "role": "user", "content": "hello", "channel": user.channel}])
        check("log_texts=None: the read-back limit is exactly 12 (12 + 0)", captured_limits == [12])
        check("log_texts=None: history ends with the raw text appended, nothing filtered out",
              captured_histories[0] == [{"role": "assistant", "content": "earlier reply"}, {"role": "user", "content": "hello"}])
        check("log_texts=None: run_message still returns the turn's reply", reply == "ok, done")

        # 6b: log_texts=[...] -- each raw text logged as its own row, those
        # rows excluded from the read-back, limit raised by the batch
        # size, and the COMBINED text (not either raw piece) is what
        # actually lands in history for the model.
        appended.clear()
        captured_limits.clear()
        captured_histories.clear()
        reply2 = await cli.run_message(
            user, agent=object(), text="COMBINED: first + second", send=None,
            log_texts=["first", "second"],
        )
        user_rows = [a for a in appended if a["role"] == "user"]
        check("log_texts=[...]: each raw text is logged as its own row, in order",
              [r["content"] for r in user_rows] == ["first", "second"])
        logged_ids = {r["id"] for r in user_rows}
        check("log_texts=[...]: the read-back limit is raised by exactly the batch size (12 + 2)",
              captured_limits == [14])

        # Simulate a real DB read-back that would otherwise include the
        # rows just logged above (this is what run_message itself must
        # filter back out) alongside one genuinely older, unrelated row.
        # The id counter is reset so THIS call's append_message calls
        # produce the exact same ids the seed data below expects -- a real
        # DB would naturally have this property (the id a row gets back
        # from an insert is the same id a subsequent read-back reports for
        # it); the fake id generator otherwise has no way to know that in
        # advance.
        id_counter = itertools.count(100)
        seed_recent[:] = [
            {"id": 1, "role": "assistant", "content": "earlier reply"},
            {"id": 100, "role": "user", "content": "first"},
            {"id": 101, "role": "user", "content": "second"},
        ]
        appended.clear()
        captured_limits.clear()
        captured_histories.clear()
        reply3 = await cli.run_message(
            user, agent=object(), text="COMBINED: first + second", send=None,
            log_texts=["first", "second"],
        )
        just_logged_ids = {r["id"] for r in appended if r["role"] == "user"}
        history_for_turn = captured_histories[0]
        check("log_texts=[...]: the just-logged raw rows are excluded from what the model sees",
              not any(h.get("content") in ("first", "second") for h in history_for_turn))
        check("log_texts=[...]: real prior context is preserved, not pushed out",
              {"role": "assistant", "content": "earlier reply"} in history_for_turn)
        check("log_texts=[...]: the COMBINED text is what's appended for the model, exactly once",
              history_for_turn.count({"role": "user", "content": "COMBINED: first + second"}) == 1)
        check("log_texts=[...]: run_message still returns the turn's reply", reply3 == "ok, done")
    finally:
        db.append_message = real_append_message
        db.get_recent_messages = real_get_recent_messages
        cli.run_turn = real_run_turn
        usage.peek_usage = real_peek_usage
        usage.check_and_consume = real_check_and_consume


async def main() -> None:
    part1_combine_batched_messages()
    await part2_leader_follower_mechanics()
    await part3_collection_ceiling()
    await part4_crash_safety()
    await part5_process_inbound_wiring()
    await part6_run_message_log_texts()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
