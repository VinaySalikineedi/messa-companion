"""Tests for Feature 2 of the "onboarding apps / reactions / message-split /
minimal-questions" round: context-based tapback reactions on inbound texts
(a bed emoji for mattress talk, a salute for a task -- swapped to a
checkmark once that task's turn actually finishes), plus the "immediate
read" fix that moves the read receipt to the START of _process_inbound
instead of the end.

Three parts:
  1. server.py's _pick_contextual_reaction -- the single-purpose LLM
     classifier: returns the task-salute for an actionable request, a
     contextually-fitting emoji otherwise, None for "NONE"/empty/garbled/
     failed/timed-out, and never calls the model at all for empty text.
  2. server.py's _react_to_inbound -- respects config.INBOUND_REACTIONS_
     ENABLED and the message_handle gate (iMessage-only, same as the
     existing react_to_message tool), sends the classifier's emoji, and
     reports back the exact task-category emoji actually sent ONLY when
     a task reaction was sent (so the caller knows to swap it for a
     checkmark later).
  3. server.py's _process_inbound -- mark_read now fires BEFORE the agent
     turn (not after), the reaction classifier runs concurrently (started
     right alongside mark_read, awaited only after the reply is fully
     sent), and a non-None task-category result triggers the
     salute -> checkmark swap; a non-task reaction or no reaction at all
     never touches send_reaction again after the first tapback.
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
# technique as test_disconnect_switch.py / test_app_connect_queue.py.
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


class FakeResult:
    def __init__(self, content):
        self.content = content


class FakeModel:
    def __init__(self, content=None, exc=None, delay=0.0):
        self.content = content
        self.exc = exc
        self.delay = delay
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc:
            raise self.exc
        return FakeResult(self.content)


# --- Part 1: _pick_contextual_reaction ---

async def part1_pick_contextual_reaction():
    real_build_model = config.build_model
    real_timeout = config.REACTION_CLASSIFIER_TIMEOUT_SECONDS

    try:
        # 1a: empty text never even calls the model.
        called = {"n": 0}

        def fake_build_model_should_not_be_called(*a, **kw):
            called["n"] += 1
            return FakeModel(content="🛏️")
        config.build_model = fake_build_model_should_not_be_called

        result = await server._pick_contextual_reaction("")
        check("empty text returns None without ever calling the model", result is None and called["n"] == 0)
        result2 = await server._pick_contextual_reaction("   ")
        check("whitespace-only text also skips the model call", result2 is None and called["n"] == 0)

        # 1b: a task-shaped message gets the fixed salute emoji.
        fake_model = FakeModel(content=server._TASK_REACTION_EMOJI)
        config.build_model = lambda *a, **kw: fake_model
        result3 = await server._pick_contextual_reaction("can you book me a haircut for friday")
        check("an actionable request returns the fixed task-salute emoji",
              result3 == server._TASK_REACTION_EMOJI)

        # 1c: a topical (non-task) message gets whatever emoji the model picks.
        fake_model2 = FakeModel(content="🛏️")
        config.build_model = lambda *a, **kw: fake_model2
        result4 = await server._pick_contextual_reaction("ugh my mattress is killing my back")
        check("a topical message returns the model's own chosen emoji", result4 == "🛏️")

        # 1d: the model explicitly says nothing fits.
        fake_model3 = FakeModel(content="NONE")
        config.build_model = lambda *a, **kw: fake_model3
        result5 = await server._pick_contextual_reaction("ok sounds good")
        check("'NONE' (any case) from the model means no reaction", result5 is None)
        fake_model3b = FakeModel(content="none")
        config.build_model = lambda *a, **kw: fake_model3b
        check("'none' lowercase is also treated as no reaction",
              await server._pick_contextual_reaction("ok") is None)

        # 1e: a garbled/over-long reply is rejected defensively rather than sent as-is.
        fake_model4 = FakeModel(content="Sure, here you go: 🛏️ hope that helps!")
        config.build_model = lambda *a, **kw: fake_model4
        result6 = await server._pick_contextual_reaction("my back hurts")
        check("an over-long/garbled classifier reply is rejected, not sent verbatim", result6 is None)

        # 1f: any exception from the model call -> None, never raises.
        fake_model5 = FakeModel(exc=RuntimeError("boom"))
        config.build_model = lambda *a, **kw: fake_model5
        result7 = await server._pick_contextual_reaction("whatever")
        check("a classifier call that raises is swallowed, returns None (never propagates)", result7 is None)

        # 1g: a slow/hung model call is cut off by the configured timeout.
        config.REACTION_CLASSIFIER_TIMEOUT_SECONDS = 0.05
        fake_model6 = FakeModel(content="🛏️", delay=1.0)
        config.build_model = lambda *a, **kw: fake_model6
        result8 = await server._pick_contextual_reaction("my mattress again")
        check("a classifier call slower than REACTION_CLASSIFIER_TIMEOUT_SECONDS is abandoned, returns None",
              result8 is None)
    finally:
        config.build_model = real_build_model
        config.REACTION_CLASSIFIER_TIMEOUT_SECONDS = real_timeout


# --- Part 2: _react_to_inbound ---

async def part2_react_to_inbound():
    real_enabled = config.INBOUND_REACTIONS_ENABLED
    real_pick = server._pick_contextual_reaction
    real_send_reaction = sendblue.send_reaction

    sent = []

    async def fake_send_reaction(number, message_handle, reaction):
        sent.append((number, message_handle, reaction))
        return {}

    sendblue.send_reaction = fake_send_reaction

    try:
        # 2a: feature disabled -> never even classifies.
        config.INBOUND_REACTIONS_ENABLED = False
        pick_calls = []

        async def fake_pick_should_not_run(text):
            pick_calls.append(text)
            return "🛏️"
        server._pick_contextual_reaction = fake_pick_should_not_run

        result = await server._react_to_inbound("+15551234567", "handle-1", "my mattress hurts")
        check("disabled feature returns None and never calls the classifier",
              result is None and pick_calls == [] and sent == [])

        # 2b: enabled, but no message_handle (SMS/RCS/CLI) -> no reaction at all.
        config.INBOUND_REACTIONS_ENABLED = True
        result2 = await server._react_to_inbound("+15551234567", None, "my mattress hurts")
        check("no message_handle returns None and never calls the classifier (SMS has no tapbacks)",
              result2 is None and pick_calls == [] and sent == [])

        # 2c: classifier finds nothing -> no Sendblue call, returns False.
        async def fake_pick_none(text):
            return None
        server._pick_contextual_reaction = fake_pick_none
        result3 = await server._react_to_inbound("+15551234567", "handle-1", "ok thanks")
        check("classifier returning None sends nothing and returns None", result3 is None and sent == [])

        # 2d: classifier returns the task salute -> sent, returns True.
        sent.clear()

        async def fake_pick_task(text):
            return server._TASK_REACTION_EMOJI
        server._pick_contextual_reaction = fake_pick_task
        result4 = await server._react_to_inbound("+15551234567", "handle-1", "book my flight please")
        check("a task reaction is actually sent via sendblue.send_reaction",
              sent == [("+15551234567", "handle-1", server._TASK_REACTION_EMOJI)])
        check("a task reaction reports the exact emoji sent (caller should swap it for a checkmark later)",
              result4 == server._TASK_REACTION_EMOJI)

        # 2e: classifier returns a non-task emoji -> sent, but returns False
        # (nothing to swap to a checkmark for a non-task reaction).
        sent.clear()

        async def fake_pick_bed(text):
            return "🛏️"
        server._pick_contextual_reaction = fake_pick_bed
        result5 = await server._react_to_inbound("+15551234567", "handle-1", "my mattress hurts")
        check("a non-task reaction is still sent", sent == [("+15551234567", "handle-1", "🛏️")])
        check("a non-task reaction reports None (nothing to swap to a checkmark)", result5 is None)

        # 2f: the Sendblue call itself fails -> swallowed, returns False.
        sent.clear()

        async def failing_send_reaction(number, message_handle, reaction):
            raise SendblueError("boom")
        sendblue.send_reaction = failing_send_reaction
        server._pick_contextual_reaction = fake_pick_task
        result6 = await server._react_to_inbound("+15551234567", "handle-1", "book my flight please")
        check("a failed Sendblue send is swallowed and returns None, never raises", result6 is None)
    finally:
        config.INBOUND_REACTIONS_ENABLED = real_enabled
        server._pick_contextual_reaction = real_pick
        sendblue.send_reaction = real_send_reaction


# --- Part 3: _process_inbound ordering + checkmark swap ---

async def part3_process_inbound_ordering_and_swap():
    from messa import waitlist as waitlist_mod
    from messa.agents import registry as registry_mod

    real_admission = waitlist_mod.check_new_user_admission
    real_send_typing = sendblue.send_typing_indicator
    real_mark_read = sendblue.mark_read
    real_send_reaction = sendblue.send_reaction
    real_send_message = sendblue.send_message
    real_load_context = server.cli.load_user_context
    real_run_message = server.cli.run_message
    real_build_orchestrator = server.build_orchestrator
    real_react_to_inbound = server._react_to_inbound

    order = []
    reaction_calls = []

    async def fake_admission(number):
        return waitlist_mod.AdmissionDecision(allowed=True)

    async def fake_send_typing(number):
        return {}

    async def fake_mark_read(number):
        order.append("mark_read")
        return {}

    async def fake_send_reaction(number, message_handle, reaction):
        reaction_calls.append(reaction)
        return {}

    async def fake_send_message(number, content, **kw):
        return {}

    async def fake_load_context(number, name=None, channel="sms", message_handle=None):
        order.append("load_context")
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

    try:
        # 3a: a task message -- mark_read fires BEFORE run_message starts and
        # finishes, and the salute-swap happens only AFTER run_message ends.
        order.clear()
        reaction_calls.clear()

        async def fake_run_message_task(user, agent, text, send=None, log_texts=None, on_turn_complete=None):
            order.append("run_message_start")
            await asyncio.sleep(0.02)
            order.append("run_message_end")
            return "done"

        server.cli.run_message = fake_run_message_task

        async def fake_react_task(number, message_handle, text):
            order.append("reaction_task_scheduled")
            await asyncio.sleep(0.01)  # resolves WHILE run_message is still running
            order.append("reaction_task_resolved")
            return server._TASK_REACTION_EMOJI  # this was a task -- caller should swap to a checkmark

        server._react_to_inbound = fake_react_task

        await server._process_inbound("+15551234567", "book my flight", "sms", message_handle="handle-1")

        check("mark_read fires before load_user_context/run_message (immediate read)",
              order.index("mark_read") < order.index("load_context")
              and order.index("mark_read") < order.index("run_message_start"))
        check("the reaction task resolves concurrently, not blocking run_message",
              order.index("reaction_task_resolved") < order.index("run_message_end"))
        check("run_message still fully completes normally",
              "run_message_start" in order and "run_message_end" in order)
        check("a task reaction (non-None) triggers exactly the salute-removal then the checkmark, in order",
              reaction_calls == [f"-{server._TASK_REACTION_EMOJI}", "✅"])

        # 3b: a non-task (or no) reaction -- no swap calls at all afterward.
        order.clear()
        reaction_calls.clear()

        async def fake_react_non_task(number, message_handle, text):
            return None

        server._react_to_inbound = fake_react_non_task
        await server._process_inbound("+15551234567", "my mattress hurts", "sms", message_handle="handle-1")
        check("a non-task reaction result never triggers any checkmark-swap Sendblue calls",
              reaction_calls == [])

        # 3c: the reaction task itself blows up -- swallowed, no crash, no swap.
        order.clear()
        reaction_calls.clear()

        async def fake_react_raises(number, message_handle, text):
            raise RuntimeError("classifier exploded")

        server._react_to_inbound = fake_react_raises
        try:
            await server._process_inbound("+15551234567", "anything", "sms", message_handle="handle-1")
            crashed = False
        except Exception:
            crashed = True
        check("a reaction task that raises never crashes _process_inbound", crashed is False)
        check("a crashed reaction task never triggers a checkmark swap", reaction_calls == [])

        # 3d: no message_handle at all (plain SMS/RCS) -- _process_inbound
        # still calls _react_to_inbound (which internally no-ops on its own),
        # and mark_read still fires immediately either way.
        order.clear()
        reaction_calls.clear()
        react_called_with = []

        async def fake_react_records(number, message_handle, text):
            react_called_with.append(message_handle)
            return None

        server._react_to_inbound = fake_react_records
        await server._process_inbound("+15551234567", "hello", "sms", message_handle=None)
        check("mark_read still fires immediately for a plain SMS/RCS thread with no message_handle",
              order and order[0] == "mark_read")
        check("the reaction path is still invoked (it internally no-ops on None message_handle)",
              react_called_with == [None])
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


async def main() -> None:
    await part1_pick_contextual_reaction()
    await part2_react_to_inbound()
    await part3_process_inbound_ordering_and_swap()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
