"""Tests for V3-autonomous.md Phase 1 (docs/V3-autonomous.md, section 6 --
"One-Touch Approvals & Conflict Auto-Supersede"), built on top of
feature/agentic-upgrade:

  1. reliability.is_bare_affirmation -- the pure heuristic that flags a
     short, purely-affirmative reply ("yes", "Approved.", or real content
     followed by a short affirmation, e.g. "I like this version! Approved"
     -- the actual field-incident shape), while leaving anything with
     substantive extra content (a correction, a question, a long message
     that merely contains the word "yes") alone.
  2. db._auto_supersede_conflicting_routine + its wiring into
     db._insert_cron_job -- deterministic, zero-token recipient-collision
     detection: a brand-new routine that shares a recipient email with
     EXACTLY ONE other active/paused routine cancels that older one; zero
     or multiple candidates touch nothing (same safety rule as
     db._retire_legacy_briefing_if_any). Also confirms the
     CONFLICT_AUTO_SUPERSEDE_ENABLED kill switch.
  3. registry.py's confirm_pending_action tool -- surfaces a plain-language
     note when db.confirm_pending_action's result carries a
     "_superseded_job" key (create_routine only), and stays byte-identical
     to before this feature for every other action_type / when nothing was
     superseded.
  4. cli.py's run_message -- the structural one-touch-approval backstop:
     a bare affirmation with a still-pending action and no
     confirm_pending_action/reject_pending_action tool call this turn
     retries once with a nudge (same bounded-single-retry shape as
     run_turn's own _STALL_PATTERN/unverified_claim_reason backstops);
     already-confirmed turns, non-affirmations, no pending actions, and
     the ONE_TOUCH_APPROVAL_NUDGE_ENABLED kill switch all leave it at
     exactly one run_turn call.

No live Postgres, no live LLM call.
"""
import asyncio
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

from datetime import datetime, timezone  # noqa: E402

from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402

from messa import cli, config, db, reliability, usage  # noqa: E402
from messa.agents import registry  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _user(**kwargs) -> config.UserContext:
    base = dict(user_id=1, phone_number="+15550000000", name="Jane", onboarding_step="complete")
    base.update(kwargs)
    return config.UserContext(**base)


# ---------------------------------------------------------------------------
# Fakes -- same FakeAcquire/FakePool/FakeConn/FakeRow shape as
# test_workspace_asset_registry.py / test_scratchpad_and_skills.py.
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, fetch_queue=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.fetch_queue = list(fetch_queue or [])
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


# ---------------------------------------------------------------------------
# Part 1: reliability.is_bare_affirmation
# ---------------------------------------------------------------------------

def part1_is_bare_affirmation():
    cases = [
        ("Approved", True, "bare affirmation word alone"),
        ("approved.", True, "trailing punctuation doesn't matter"),
        ("yes", True, "the simplest case"),
        ("Yes!", True, "capitalized, with punctuation"),
        ("I like this version! Approved", True,
         "the actual field-incident shape: real content, then a short, clear affirmation"),
        ("sounds good", True, "two-word affirmation phrase"),
        ("send it", True), ("go ahead", True), ("lock it in", True), ("lgtm", True),
        ("yes but move it to 3pm", False, "an affirmation word followed by a real correction"),
        ("yesterday I approved this contract", False, "the word 'yes'/'approved' inside unrelated prose"),
        ("no", False, "a bare rejection is not an affirmation"),
        ("what time is my flight tomorrow", False, "an ordinary question"),
        ("", False, "empty string"),
        (None, False, "None"),
        ("Book me a flight to SFO", False, "an ordinary new request"),
    ]
    for text, expected, *why in cases:
        got = reliability.is_bare_affirmation(text)
        reason = f" ({why[0]})" if why else ""
        check(f"is_bare_affirmation({text!r}) is {expected}{reason}", got is expected)


# ---------------------------------------------------------------------------
# Part 2: db._auto_supersede_conflicting_routine + _insert_cron_job wiring
# ---------------------------------------------------------------------------

async def part2_auto_supersede_conflicting_routine():
    try:
        # 2a: no email in the new routine's prompt -- no-op, never even
        # queries for candidates.
        conn = FakeConn()
        install_fake_pool(conn)
        result = await db._auto_supersede_conflicting_routine(conn, 1, 99, "check my calendar every morning")
        check("no email in prompt_or_task: returns None", result is None)
        check("no email in prompt_or_task: never queries for candidates", conn.calls == [])

        # 2b: email present, but zero other routines mention it -- no-op.
        conn = FakeConn(fetch_queue=[[]])
        result = await db._auto_supersede_conflicting_routine(
            conn, 1, 99, "follow up with kj@mangustacap.com about the term sheet",
        )
        check("email present, zero candidates: returns None", result is None)

        # 2c: exactly one other routine mentions the same email -- cancelled.
        old_row = FakeRow(id=42, prompt_or_task="email kj@mangustacap.com every day about the deal", status="active")
        cancelled_row = FakeRow(id=42, prompt_or_task=old_row["prompt_or_task"], status="cancelled")
        conn = FakeConn(fetch_queue=[[old_row]], fetchrow_queue=[cancelled_row])
        result = await db._auto_supersede_conflicting_routine(
            conn, 1, 99, "follow up with kj@mangustacap.com about the term sheet",
        )
        check("exactly one match: the older routine is cancelled and returned", result == cancelled_row)
        update_calls = [c for c in conn.calls if c[0] == "fetchrow" and "UPDATE cron_jobs" in c[1]]
        check("exactly one match: issues exactly one UPDATE...cancelled call", len(update_calls) == 1)
        check("exactly one match: cancels the OLD routine's id, not the new one",
              update_calls[0][2] == (42,))

        # 2d: TWO other routines mention the same email -- genuinely
        # ambiguous, so nothing is touched (same safety rule as
        # _retire_legacy_briefing_if_any).
        row_a = FakeRow(id=42, prompt_or_task="email kj@mangustacap.com weekly", status="active")
        row_b = FakeRow(id=43, prompt_or_task="also remind me about kj@mangustacap.com", status="paused")
        conn = FakeConn(fetch_queue=[[row_a, row_b]])
        result = await db._auto_supersede_conflicting_routine(
            conn, 1, 99, "follow up with kj@mangustacap.com about the term sheet",
        )
        check("two ambiguous matches: returns None (nothing cancelled)", result is None)
        update_calls = [c for c in conn.calls if c[0] == "fetchrow"]
        check("two ambiguous matches: no UPDATE ever issued", update_calls == [])

        # 2e: a candidate that mentions a DIFFERENT email entirely never matches.
        other_row = FakeRow(id=7, prompt_or_task="email someone-else@example.com weekly", status="active")
        conn = FakeConn(fetch_queue=[[other_row]])
        result = await db._auto_supersede_conflicting_routine(
            conn, 1, 99, "follow up with kj@mangustacap.com about the term sheet",
        )
        check("a candidate mentioning a different email entirely: no match", result is None)
    finally:
        pass


async def part3_insert_cron_job_wiring():
    real_flag = config.CONFLICT_AUTO_SUPERSEDE_ENABLED
    try:
        payload = {
            "prompt_or_task": "email kj@mangustacap.com the updated term sheet",
            "cron_expression": "once",
            "user_timezone": "UTC",
            "next_run_at": datetime.now(timezone.utc).isoformat(),
            "execution_mode": "autonomous",
            "meta": {},
        }
        new_row = FakeRow(id=99, prompt_or_task=payload["prompt_or_task"], status="active")
        old_row = FakeRow(id=42, prompt_or_task="email kj@mangustacap.com the draft", status="active")
        cancelled_row = FakeRow(id=42, prompt_or_task=old_row["prompt_or_task"], status="cancelled")

        # 3a: flag ON, exactly one conflicting routine -- the returned dict
        # carries "_superseded_job", and the row's own fields are untouched.
        config.CONFLICT_AUTO_SUPERSEDE_ENABLED = True
        conn = FakeConn(fetchrow_queue=[new_row, cancelled_row], fetch_queue=[[old_row]])
        result = await db._insert_cron_job(conn, 1, payload)
        check("flag on + one conflict: the new job's own row fields are returned untouched",
              result["id"] == 99 and result["prompt_or_task"] == payload["prompt_or_task"])
        check("flag on + one conflict: '_superseded_job' key is present and correct",
              result.get("_superseded_job") == cancelled_row)

        # 3b: flag ON, no conflicts -- no "_superseded_job" key at all
        # (never set to None -- see db._insert_cron_job's own docstring).
        conn = FakeConn(fetchrow_queue=[new_row], fetch_queue=[[]])
        result = await db._insert_cron_job(conn, 1, payload)
        check("flag on + no conflict: '_superseded_job' key is absent entirely",
              "_superseded_job" not in result)

        # 3c: flag OFF -- the supersede check never even runs, regardless
        # of what a real conflict lookup would have found.
        config.CONFLICT_AUTO_SUPERSEDE_ENABLED = False
        conn = FakeConn(fetchrow_queue=[new_row])
        result = await db._insert_cron_job(conn, 1, payload)
        check("flag off: '_superseded_job' key is absent", "_superseded_job" not in result)
        check("flag off: the conflict-lookup query never runs at all",
              all("SELECT id, prompt_or_task FROM cron_jobs" not in c[1] for c in conn.calls))
    finally:
        config.CONFLICT_AUTO_SUPERSEDE_ENABLED = real_flag


# ---------------------------------------------------------------------------
# Part 4: registry.py's confirm_pending_action tool surfacing the note
# ---------------------------------------------------------------------------

async def part4_confirm_pending_action_surfaces_supersede_note():
    real_confirm = db.confirm_pending_action
    try:
        user = _user(user_id=5)
        tools = registry.build_orchestrator_tools(user)
        confirm_tool = next(t for t in tools if t.name == "confirm_pending_action")

        # 4a: a create_routine confirmation that superseded another routine
        # -- the note is appended, naming the cancelled routine.
        async def fake_confirm_with_supersede(user_id, pending_action_id):
            return {
                "ok": True,
                "action_type": "create_routine",
                "result": {
                    "id": 99, "prompt_or_task": "email kj@mangustacap.com the updated term sheet",
                    "_superseded_job": {"id": 42, "prompt_or_task": "email kj@mangustacap.com the draft"},
                },
            }
        db.confirm_pending_action = fake_confirm_with_supersede
        reply = await confirm_tool.coroutine(pending_action_id=7)
        check("create_routine + supersede: base confirmation text is still present",
              "Confirmed and applied action #7" in reply)
        check("create_routine + supersede: mentions the cancelled routine's id",
              "#42" in reply)
        check("create_routine + supersede: names what it was replaced by reasoning (same recipient)",
              "replaces" in reply.lower() or "cancelled" in reply.lower())

        # 4b: a create_routine confirmation with NO conflict -- byte-plain
        # base message, no stray note.
        async def fake_confirm_no_supersede(user_id, pending_action_id):
            return {
                "ok": True, "action_type": "create_routine",
                "result": {"id": 100, "prompt_or_task": "daily briefing"},
            }
        db.confirm_pending_action = fake_confirm_no_supersede
        reply2 = await confirm_tool.coroutine(pending_action_id=8)
        check("create_routine, no supersede: exactly the base confirmation, nothing extra",
              reply2 == "Confirmed and applied action #8 (create_routine).")

        # 4c: a totally unrelated action_type (e.g. create_calendar_event)
        # is completely unaffected by this change.
        async def fake_confirm_other(user_id, pending_action_id):
            return {"ok": True, "action_type": "create_calendar_event", "result": {"id": 1}}
        db.confirm_pending_action = fake_confirm_other
        reply3 = await confirm_tool.coroutine(pending_action_id=9)
        check("an unrelated action_type: unaffected, exactly the base message",
              reply3 == "Confirmed and applied action #9 (create_calendar_event).")
    finally:
        db.confirm_pending_action = real_confirm


# ---------------------------------------------------------------------------
# Part 5: cli.run_message's one-touch-approval nudge
# ---------------------------------------------------------------------------

def _install_run_message_fakes():
    """Same fakes as test_message_splitting.py's part2, minus the ones this
    part doesn't need -- returns everything a caller needs to restore in
    its own finally block, plus the shared mutable state (run_turn_calls,
    sent, pending_actions_calls) the test bodies inspect."""
    real = {
        "run_turn": cli.run_turn,
        "apply_guardrail": cli._apply_reply_guardrail,
        "append_message": db.append_message,
        "get_recent_messages": db.get_recent_messages,
        "get_user_by_id": db.get_user_by_id,
        "list_pending_actions": db.list_pending_actions,
        "peek_usage": usage.peek_usage,
        "check_and_consume": usage.check_and_consume,
        "claim_daily_notice": usage.claim_daily_notice,
    }

    async def fake_append_message(user_id, role, content, channel=None):
        return 1

    async def fake_get_recent_messages(*a, **kw):
        return []

    async def fake_get_user_by_id(user_id):
        return {"onboarding_step": "complete"}

    async def identity_guardrail(msg_text):
        return msg_text

    async def fake_peek_usage(user, feature):
        return usage.LimitResult(allowed=True, feature=feature, count=0, limit=1000, plan_name="test")

    async def fake_check_and_consume(user, feature, amount=1):
        return usage.LimitResult(allowed=True, feature=feature, count=1, limit=1000, plan_name="test")

    async def fake_claim_notice(user, feature):
        return True

    db.append_message = fake_append_message
    db.get_recent_messages = fake_get_recent_messages
    db.get_user_by_id = fake_get_user_by_id
    cli._apply_reply_guardrail = identity_guardrail
    usage.peek_usage = fake_peek_usage
    usage.check_and_consume = fake_check_and_consume
    usage.claim_daily_notice = fake_claim_notice

    return real


def _restore_run_message_fakes(real):
    cli.run_turn = real["run_turn"]
    cli._apply_reply_guardrail = real["apply_guardrail"]
    db.append_message = real["append_message"]
    db.get_recent_messages = real["get_recent_messages"]
    db.get_user_by_id = real["get_user_by_id"]
    db.list_pending_actions = real["list_pending_actions"]
    usage.peek_usage = real["peek_usage"]
    usage.check_and_consume = real["check_and_consume"]
    usage.claim_daily_notice = real["claim_daily_notice"]


async def part5_one_touch_approval_nudge():
    real = _install_run_message_fakes()
    real_flag = config.ONE_TOUCH_APPROVAL_NUDGE_ENABLED
    try:
        config.ONE_TOUCH_APPROVAL_NUDGE_ENABLED = True

        # --- 5a: bare affirmation + a pending action + the model's first
        # reply never confirmed/rejected anything -- retries exactly once
        # with a nudge, and the nudge's corrected reply is what gets sent. ---
        run_turn_calls = []

        async def fake_run_turn_first_misses(agent, messages, on_ai_message=None, _allow_retry=True):
            run_turn_calls.append(list(messages))
            if len(run_turn_calls) == 1:
                text = "Great, glad you like it!"
                await on_ai_message(text)
                return list(messages) + [AIMessage(content=text)]
            text = "Confirmed and applied action #7 (create_routine)."
            await on_ai_message(text)
            return list(messages) + [AIMessage(
                content=text,
                tool_calls=[{"name": "confirm_pending_action", "args": {"pending_action_id": 7}, "id": "c1"}],
            )]

        cli.run_turn = fake_run_turn_first_misses

        async def fake_list_pending_one(user_id, state="pending"):
            return [{"id": 7, "action_type": "create_routine"}]

        db.list_pending_actions = fake_list_pending_one

        sent = []

        async def fake_send(text):
            sent.append(text)

        user = _user(user_id=20)
        await cli.run_message(user, agent=object(), text="I like this version! Approved", send=fake_send)

        check("5a: run_turn is called exactly twice (one bounded retry)", len(run_turn_calls) == 2)
        check("5a: the second call's extra message is a HumanMessage nudge, not from the user",
              isinstance(run_turn_calls[1][-1], HumanMessage) and "auto-check" in run_turn_calls[1][-1].content)
        check("5a: the nudge explicitly says it's not from the user",
              "not from the user" in run_turn_calls[1][-1].content)
        check("5a: the corrected (confirming) reply is what actually reaches the user",
              sent == ["Great, glad you like it!", "Confirmed and applied action #7 (create_routine)."])

        # --- 5b: same bare affirmation + pending action, but the model DID
        # call confirm_pending_action on the first try -- no retry needed. ---
        run_turn_calls2 = []

        async def fake_run_turn_confirms_immediately(agent, messages, on_ai_message=None, _allow_retry=True):
            run_turn_calls2.append(list(messages))
            text = "Confirmed and applied action #7 (create_routine)."
            await on_ai_message(text)
            return list(messages) + [AIMessage(
                content=text,
                tool_calls=[{"name": "confirm_pending_action", "args": {"pending_action_id": 7}, "id": "c1"}],
            )]

        cli.run_turn = fake_run_turn_confirms_immediately
        user2 = _user(user_id=21)
        await cli.run_message(user2, agent=object(), text="Approved", send=fake_send)
        check("5b: already confirmed on the first try -- run_turn called exactly once, no nudge",
              len(run_turn_calls2) == 1)

        # --- 5c: bare affirmation, but there is NOTHING actually pending
        # -- no retry (nothing to nudge about). ---
        run_turn_calls3 = []

        async def fake_run_turn_no_confirm(agent, messages, on_ai_message=None, _allow_retry=True):
            run_turn_calls3.append(list(messages))
            text = "Sounds good, anything else?"
            await on_ai_message(text)
            return list(messages) + [AIMessage(content=text)]

        cli.run_turn = fake_run_turn_no_confirm

        async def fake_list_pending_empty(user_id, state="pending"):
            return []

        db.list_pending_actions = fake_list_pending_empty
        user3 = _user(user_id=22)
        await cli.run_message(user3, agent=object(), text="yes", send=fake_send)
        check("5c: no pending action at all -- run_turn called exactly once, no nudge",
              len(run_turn_calls3) == 1)

        # --- 5d: an ordinary, non-affirmation message never triggers this
        # check at all, regardless of pending actions. ---
        run_turn_calls4 = []

        async def fake_run_turn_ordinary(agent, messages, on_ai_message=None, _allow_retry=True):
            run_turn_calls4.append(list(messages))
            text = "Sure, checking your calendar now."
            await on_ai_message(text)
            return list(messages) + [AIMessage(content=text)]

        cli.run_turn = fake_run_turn_ordinary
        db.list_pending_actions = fake_list_pending_one  # even WITH a pending action
        user4 = _user(user_id=23)
        await cli.run_message(user4, agent=object(), text="what's on my calendar tomorrow", send=fake_send)
        check("5d: an ordinary non-affirmation message -- run_turn called exactly once",
              len(run_turn_calls4) == 1)

        # --- 5e: the kill switch -- flag off leaves the exact 5a scenario
        # (bare affirmation, pending action, no confirm call) at one call. ---
        config.ONE_TOUCH_APPROVAL_NUDGE_ENABLED = False
        run_turn_calls5 = []

        async def fake_run_turn_would_nudge(agent, messages, on_ai_message=None, _allow_retry=True):
            run_turn_calls5.append(list(messages))
            text = "Great, glad you like it!"
            await on_ai_message(text)
            return list(messages) + [AIMessage(content=text)]

        cli.run_turn = fake_run_turn_would_nudge
        db.list_pending_actions = fake_list_pending_one
        user5 = _user(user_id=24)
        await cli.run_message(user5, agent=object(), text="Approved", send=fake_send)
        check("5e: ONE_TOUCH_APPROVAL_NUDGE_ENABLED=false -- run_turn called exactly once, no nudge",
              len(run_turn_calls5) == 1)
    finally:
        config.ONE_TOUCH_APPROVAL_NUDGE_ENABLED = real_flag
        _restore_run_message_fakes(real)


async def main() -> None:
    part1_is_bare_affirmation()
    await part2_auto_supersede_conflicting_routine()
    await part3_insert_cron_job_wiring()
    await part4_confirm_pending_action_surfaces_supersede_note()
    await part5_one_touch_approval_nudge()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
