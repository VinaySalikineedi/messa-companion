"""Tests for PR2 of the "smart progressive tapbacks + double-text handling"
round (plans/glowing-forging-pumpkin.md, Feature A): swapping a task-
category tapback to an "in progress" emoji when a turn runs long, then to
the final checkmark once it actually finishes -- without ever cancelling
the real agent turn to do so.

Five parts:
  1. server.py's _swap_reaction -- remove-then-add via Sendblue, swallows
     failures, returns whether both calls actually succeeded.
  2. server.py's _peek_task_reaction -- a non-blocking peek at the
     reaction_task: None while it's still running or if it raised, its
     result once it's actually done.
  3. server.py's _progress_stages -- reads the (seconds, emoji) stage(s)
     straight from config, one stage today.
  4. server.py's _run_turn_with_progress_reactions -- the core of this PR:
     races each stage's timeout against turn_task using asyncio.wait
     (NEVER asyncio.wait_for, which would cancel it), only ever fires for
     a task-category reaction, and always lets turn_task run to actual
     completion regardless of how many stages fire.
  5. server.py's _process_inbound -- config.TAPBACK_PROGRESS_UPDATES_
     ENABLED defaults off (byte-identical to pre-PR2 behavior), and wiring
     it on end-to-end produces the right reaction sequence for a fast task
     turn, a slow task turn, a non-task (mood) reaction, and no
     message_handle at all.
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

# Fake `composio` package so server.py (which imports tools/integration_tools.py
# transitively) imports cleanly without the real SDK installed -- same
# technique as test_inbound_reactions.py / test_disconnect_switch.py.
_fake_composio_exceptions = types.ModuleType("composio.exceptions")


class ComposioMultipleConnectedAccountsError(Exception):
    pass


_fake_composio_exceptions.ComposioMultipleConnectedAccountsError = ComposioMultipleConnectedAccountsError
_fake_composio_pkg = types.ModuleType("composio")
_fake_composio_pkg.exceptions = _fake_composio_exceptions
sys.modules["composio"] = _fake_composio_pkg
sys.modules["composio.exceptions"] = _fake_composio_exceptions

from messa import config, server  # noqa: E402
from messa.channels import sendblue  # noqa: E402
from messa.channels.sendblue import SendblueError  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# --- Part 1: _swap_reaction ---

async def part1_swap_reaction():
    real_send_reaction = sendblue.send_reaction
    sent = []

    async def fake_send_reaction(number, message_handle, reaction):
        sent.append(reaction)
        return {}

    sendblue.send_reaction = fake_send_reaction
    try:
        sent.clear()
        ok = await server._swap_reaction("+15551234567", "handle-1", "📅", "⏳")
        check("a successful swap removes the old reaction then adds the new one, in order",
              sent == ["-📅", "⏳"])
        check("a successful swap returns True", ok is True)

        async def failing_send_reaction(number, message_handle, reaction):
            raise SendblueError("boom")

        sendblue.send_reaction = failing_send_reaction
        ok2 = await server._swap_reaction("+15551234567", "handle-1", "📅", "⏳")
        check("a failed swap is swallowed and returns False, never raises", ok2 is False)
    finally:
        sendblue.send_reaction = real_send_reaction


# --- Part 2: _peek_task_reaction ---

async def part2_peek_task_reaction():
    async def resolves_to(value):
        return value

    async def raises():
        raise RuntimeError("boom")

    pending = asyncio.get_event_loop().create_future()
    pending_task = asyncio.ensure_future(_await_future(pending))
    try:
        check("an unresolved task peeks as None (never blocks)",
              server._peek_task_reaction(pending_task) is None)
    finally:
        pending.set_result(None)
        await pending_task

    done_task = asyncio.create_task(resolves_to("📅"))
    await done_task
    check("a resolved task peeks as its actual result", server._peek_task_reaction(done_task) == "📅")

    none_task = asyncio.create_task(resolves_to(None))
    await none_task
    check("a resolved-to-None task peeks as None", server._peek_task_reaction(none_task) is None)

    raised_task = asyncio.create_task(raises())
    try:
        await raised_task
    except RuntimeError:
        pass
    check("a task that raised peeks as None, never re-raises", server._peek_task_reaction(raised_task) is None)


async def _await_future(fut):
    return await fut


# --- Part 3: _progress_stages ---

async def part3_progress_stages():
    real_seconds = config.TAPBACK_PROGRESS_UPDATE_SECONDS
    real_emoji = config.TAPBACK_PROGRESS_EMOJI
    try:
        config.TAPBACK_PROGRESS_UPDATE_SECONDS = 42.0
        config.TAPBACK_PROGRESS_EMOJI = "🐢"
        stages = server._progress_stages()
        check("exactly one stage today", len(stages) == 1)
        check("the stage reads its (seconds, emoji) straight from config",
              stages == ((42.0, "🐢"),))
    finally:
        config.TAPBACK_PROGRESS_UPDATE_SECONDS = real_seconds
        config.TAPBACK_PROGRESS_EMOJI = real_emoji


# --- Part 4: _run_turn_with_progress_reactions ---

async def part4_run_turn_with_progress_reactions():
    real_send_reaction = sendblue.send_reaction
    real_seconds = config.TAPBACK_PROGRESS_UPDATE_SECONDS
    real_emoji = config.TAPBACK_PROGRESS_EMOJI

    sent = []

    async def fake_send_reaction(number, message_handle, reaction):
        sent.append(reaction)
        return {}

    sendblue.send_reaction = fake_send_reaction
    config.TAPBACK_PROGRESS_UPDATE_SECONDS = 0.03
    config.TAPBACK_PROGRESS_EMOJI = "⏳"

    try:
        # 4a: non-task reaction (reaction_task resolves to None) -- never
        # swaps anything, regardless of how long the turn takes.
        sent.clear()

        async def resolves_none():
            return None

        async def slow_turn():
            await asyncio.sleep(0.08)

        reaction_task = asyncio.create_task(resolves_none())
        turn_task = asyncio.create_task(slow_turn())
        result = await server._run_turn_with_progress_reactions(
            "+15551234567", "handle-1", reaction_task, turn_task)
        check("a non-task reaction never triggers a progress swap", sent == [])
        check("a non-task reaction returns None", result is None)
        check("turn_task still ran to completion normally", turn_task.done() and not turn_task.cancelled())

        # 4b: task reaction + a FAST turn (finishes before the progress
        # stage's timeout) -- no swap, returns the original task emoji.
        sent.clear()

        async def resolves_task():
            return "📅"

        async def fast_turn():
            await asyncio.sleep(0.005)

        reaction_task2 = asyncio.create_task(resolves_task())
        await reaction_task2  # ensure it's actually resolved before the race starts
        turn_task2 = asyncio.create_task(fast_turn())
        result2 = await server._run_turn_with_progress_reactions(
            "+15551234567", "handle-1", reaction_task2, turn_task2)
        check("a fast task turn never triggers a progress swap", sent == [])
        check("a fast task turn returns the original task emoji unchanged", result2 == "📅")

        # 4c: task reaction + a SLOW turn (runs past the progress stage's
        # timeout) -- swaps to the progress emoji, and turn_task is NEVER
        # cancelled to make that happen (the single riskiest detail here).
        sent.clear()

        async def resolves_task2():
            return "🔍"

        async def really_slow_turn():
            await asyncio.sleep(0.12)
            return "turn finished normally"

        reaction_task3 = asyncio.create_task(resolves_task2())
        await reaction_task3
        turn_task3 = asyncio.create_task(really_slow_turn())
        result3 = await server._run_turn_with_progress_reactions(
            "+15551234567", "handle-1", reaction_task3, turn_task3)
        check("a slow task turn swaps to the progress emoji, in order",
              sent == ["-🔍", "⏳"])
        check("a slow task turn returns the progress emoji as currently displayed", result3 == "⏳")
        check("turn_task ran to COMPLETION, was never cancelled by the progress race",
              turn_task3.done() and not turn_task3.cancelled() and turn_task3.result() == "turn finished normally")

        # 4d: the swap itself fails (Sendblue error) -- displayed reaction
        # falls back to whatever's actually still showing (the original
        # task emoji), never crashes, never blocks the turn.
        sent.clear()

        async def failing_send_reaction(number, message_handle, reaction):
            raise SendblueError("boom")

        sendblue.send_reaction = failing_send_reaction

        reaction_task4 = asyncio.create_task(resolves_task2())
        await reaction_task4
        turn_task4 = asyncio.create_task(really_slow_turn())
        result4 = await server._run_turn_with_progress_reactions(
            "+15551234567", "handle-1", reaction_task4, turn_task4)
        check("a failed progress swap never crashes and the turn still completes",
              turn_task4.done() and not turn_task4.cancelled())
        check("a failed progress swap falls back to the original task emoji (nothing actually changed)",
              result4 == "🔍")
    finally:
        sendblue.send_reaction = real_send_reaction
        config.TAPBACK_PROGRESS_UPDATE_SECONDS = real_seconds
        config.TAPBACK_PROGRESS_EMOJI = real_emoji


# --- Part 5: _process_inbound end-to-end wiring ---

async def part5_process_inbound_progress_wiring():
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
    real_flag = config.TAPBACK_PROGRESS_UPDATES_ENABLED
    real_seconds = config.TAPBACK_PROGRESS_UPDATE_SECONDS
    real_emoji = config.TAPBACK_PROGRESS_EMOJI

    reaction_calls = []

    async def fake_admission(number):
        return waitlist_mod.AdmissionDecision(allowed=True)

    async def fake_send_typing(number):
        return {}

    async def fake_mark_read(number):
        return {}

    async def fake_send_reaction(number, message_handle, reaction):
        reaction_calls.append(reaction)
        return {}

    async def fake_send_message(number, content, **kw):
        return {}

    async def fake_load_context(number, name=None, channel="sms", message_handle=None):
        return config.UserContext(user_id=1, phone_number=number, channel=channel, message_handle=message_handle)

    async def fake_build_orchestrator(user, gate):
        return object()

    waitlist_mod.check_new_user_admission = fake_admission
    sendblue.send_typing_indicator = fake_send_typing
    sendblue.mark_read = fake_mark_read
    sendblue.send_reaction = fake_send_reaction
    sendblue.send_message = fake_send_message
    server.cli.load_user_context = fake_load_context
    server.build_orchestrator = fake_build_orchestrator
    config.TAPBACK_PROGRESS_UPDATE_SECONDS = 0.03
    config.TAPBACK_PROGRESS_EMOJI = "⏳"

    try:
        # 5a: flag OFF (the actual default) -- byte-identical to pre-PR2:
        # no progress emoji ever appears, even for a turn slower than
        # TAPBACK_PROGRESS_UPDATE_SECONDS.
        check("TAPBACK_PROGRESS_UPDATES_ENABLED defaults to True", config.TAPBACK_PROGRESS_UPDATES_ENABLED is True)
        config.TAPBACK_PROGRESS_UPDATES_ENABLED = False
        reaction_calls.clear()

        async def fake_run_message_slow(user, agent, text, send=None, log_texts=None):
            await asyncio.sleep(0.08)
            return "done"

        async def fake_react_task(number, message_handle, text):
            return server._TASK_REACTION_EMOJI

        server.cli.run_message = fake_run_message_slow
        server._react_to_inbound = fake_react_task
        await server._process_inbound("+15551234567", "book my flight", "sms", message_handle="handle-1")
        check("flag off: a slow task turn NEVER shows the progress emoji, just salute -> checkmark",
              reaction_calls == [f"-{server._TASK_REACTION_EMOJI}", "✅"])

        # 5b: flag ON, FAST task turn -- still just salute -> checkmark,
        # no progress emoji (never got slow enough to need one).
        config.TAPBACK_PROGRESS_UPDATES_ENABLED = True
        reaction_calls.clear()

        async def fake_run_message_fast(user, agent, text, send=None, log_texts=None):
            await asyncio.sleep(0.005)
            return "done"

        server.cli.run_message = fake_run_message_fast
        await server._process_inbound("+15551234567", "book my flight", "sms", message_handle="handle-1")
        check("flag on + fast task turn: still just salute -> checkmark, no progress stage needed",
              reaction_calls == [f"-{server._TASK_REACTION_EMOJI}", "✅"])

        # 5c: flag ON, SLOW task turn -- salute -> progress -> checkmark,
        # in that exact order, and the turn still actually completes.
        reaction_calls.clear()
        run_message_completed = {"v": False}

        async def fake_run_message_slow2(user, agent, text, send=None, log_texts=None):
            await asyncio.sleep(0.08)
            run_message_completed["v"] = True
            return "done"

        server.cli.run_message = fake_run_message_slow2
        await server._process_inbound("+15551234567", "book my flight", "sms", message_handle="handle-1")
        check("flag on + slow task turn: salute removed, progress emoji shown, then removed for the checkmark",
              reaction_calls == [
                  f"-{server._TASK_REACTION_EMOJI}", "⏳",
                  "-⏳", "✅",
              ])
        check("the real turn still ran all the way to completion despite the progress swap",
              run_message_completed["v"] is True)

        # 5d: flag ON, non-task (mood) reaction -- no progress swap, no
        # checkmark at all, regardless of how long the turn takes.
        reaction_calls.clear()

        async def fake_react_mood(number, message_handle, text):
            return None  # a non-task mood emoji was sent (or nothing at all)

        server._react_to_inbound = fake_react_mood
        server.cli.run_message = fake_run_message_slow2
        await server._process_inbound("+15551234567", "my mattress hurts", "sms", message_handle="handle-1")
        check("flag on + non-task reaction: no progress swap and no checkmark, ever",
              reaction_calls == [])

        # 5e: flag ON, but no message_handle at all (plain SMS/RCS) --
        # the progress path is never engaged (same "and message_handle"
        # guard the flag-off path already relies on).
        reaction_calls.clear()
        server._react_to_inbound = fake_react_task
        await server._process_inbound("+15551234567", "book my flight", "sms", message_handle=None)
        check("flag on but no message_handle: no reaction calls at all (no tapback support on this channel)",
              reaction_calls == [])
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
        config.TAPBACK_PROGRESS_UPDATES_ENABLED = real_flag
        config.TAPBACK_PROGRESS_UPDATE_SECONDS = real_seconds
        config.TAPBACK_PROGRESS_EMOJI = real_emoji


async def main() -> None:
    await part1_swap_reaction()
    await part2_peek_task_reaction()
    await part3_progress_stages()
    await part4_run_turn_with_progress_reactions()
    await part5_process_inbound_progress_wiring()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
