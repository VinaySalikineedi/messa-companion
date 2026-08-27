"""Executive assistant: tasks, reminders, notes, contacts, and calendar
events. See `build_executive_subagent`'s docstring for why this subagent is
assembled differently from the others (a dedicated small-loop
`CompiledSubAgent` with a deterministic, non-LLM quality check), and the
paragraphs below for the current approval-gating and timezone rules.

Approval gating (explicit product decision): SCHEDULING -- creating,
updating, or cancelling a calendar event -- is the only mutation that goes
through the propose_*/confirm flow (see db.py's pending_actions). Tasks,
reminders, notes, and contacts all write immediately. These are low-stakes
and trivially reversible with one follow-up message ("cancel that
reminder", "delete that task"), so a confirmation round-trip only adds
friction there; a calendar event is the one category where a mistaken
write is more likely to visibly collide with a real commitment someone
else can also see (a meeting, an appointment), so that's the one kept
behind an explicit human yes.

Timezone correctness: every write that carries a date/time runs through
timeutil.to_local_aware(raw, user.timezone) before it reaches the
database, so a naive time like "3pm tomorrow" is interpreted as 3pm in the
USER'S timezone, not silently assumed to be UTC (see timeutil.py's module
docstring for the bug this fixes). Every read that displays a date/time
runs through timeutil.format_local(dt, user.timezone), so what's shown
always matches the timezone the user actually lives in, with an explicit
zone label instead of a bare, unlabeled UTC timestamp.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool
from langgraph.checkpoint.memory import MemorySaver

from .. import config, db, timeutil
from ..config import UserContext
from .common import trace_all

LABEL = "executive_assistant"


def build_executive_tools(
    user: UserContext, written: Optional[list[tuple[str, datetime]]] = None
) -> list[BaseTool]:
    """`written` (optional): when given, every tool that writes a date/time
    appends (kind, resolved_datetime) to it as a side effect. Used by
    `build_executive_subagent`'s post-hoc quality check to notice a
    suspiciously-past due_date/trigger_time without re-parsing the model's
    own free-text tool-call summary. Omitted (default None) by any other
    caller that just wants the tools themselves."""
    uid = user.user_id
    log = written if written is not None else []

    def _record(kind: str, dt: Optional[datetime]) -> None:
        if dt is not None:
            log.append((kind, dt))

    # ---- reads ----

    @tool
    async def list_tasks(status: Optional[str] = None) -> str:
        """List the user's tasks. status: one of todo/in_progress/done/cancelled, or omit for all open tasks."""
        rows = await db.list_tasks(uid, status)
        if not rows:
            return "No tasks found."
        return "\n".join(
            f"#{r['id']} [{r['status']}/{r['priority']}] {r['title']}"
            + (f" (due {timeutil.format_local(r['due_date'], user.timezone)})" if r["due_date"] else "")
            for r in rows
        )

    @tool
    async def list_reminders(status: str = "pending") -> str:
        """List the user's reminders. status: pending/sent/cancelled."""
        rows = await db.list_reminders(uid, status)
        if not rows:
            return f"No {status} reminders."
        return "\n".join(
            f"#{r['id']} at {timeutil.format_local(r['trigger_time'], user.timezone)}: {r['message']}"
            for r in rows
        )

    @tool
    async def list_calendar_events(upcoming_only: bool = True) -> str:
        """List the user's calendar events."""
        rows = await db.list_calendar_events(uid, upcoming_only)
        if not rows:
            return "No events found."
        return "\n".join(
            f"#{r['id']} {r['title']}: {timeutil.format_local(r['start_time'], user.timezone)} - "
            f"{timeutil.format_local(r['end_time'], user.timezone)}"
            + (f" @ {r['location']}" if r["location"] else "")
            for r in rows
        )

    @tool
    async def list_notes(limit: int = 20) -> str:
        """List the user's most recent notes."""
        rows = await db.list_notes(uid, limit)
        if not rows:
            return "No notes found."
        return "\n".join(f"#{r['id']} ({r['created_at']}): {r['content']}" for r in rows)

    @tool
    async def list_contacts() -> str:
        """List the people/contacts the user has told Messa about."""
        rows = await db.list_people(uid)
        if not rows:
            return "No contacts found."
        return "\n".join(
            f"{r['name']}" + (f" ({r['relationship_type']})" if r["relationship_type"] else "")
            + (f" -- {r['notes']}" if r["notes"] else "")
            for r in rows
        )

    # ---- direct writes: tasks, reminders, notes, contacts -- no confirmation ----

    @tool
    async def create_task(
        title: str,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> str:
        """Create a task right away -- no confirmation needed. due_date: a
        natural-language or ISO date/time if given, interpreted in the user's
        own timezone. priority: low/medium/high/urgent."""
        parsed = timeutil.to_local_aware(due_date, user.timezone)
        row = await db.create_task(uid, title, description, parsed, priority)
        _record("task", parsed)
        when = f" (due {timeutil.format_local(parsed, user.timezone)})" if parsed else ""
        return f"Created task #{row['id']}: '{title}'{when}."

    @tool
    async def update_task(
        task_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> str:
        """Update an existing task by id right away -- no confirmation needed.
        Only non-null fields change."""
        parsed = timeutil.to_local_aware(due_date, user.timezone) if due_date else None
        row = await db.update_task(uid, task_id, title, description, parsed, status, priority)
        if not row:
            return f"No task #{task_id} found."
        _record("task", parsed)
        return f"Updated task #{task_id}."

    @tool
    async def delete_task(task_id: int) -> str:
        """Delete a task by id right away -- no confirmation needed."""
        row = await db.delete_task(uid, task_id)
        return f"Deleted task #{task_id}." if row else f"No task #{task_id} found."

    @tool
    async def create_reminder(
        trigger_time: str, message: str, checkin_for_task_id: Optional[int] = None
    ) -> str:
        """Create a reminder right away -- no confirmation needed. trigger_time
        is interpreted in the user's own timezone if it has no explicit UTC
        offset (e.g. '3pm tomorrow' means 3pm where the user actually lives)."""
        parsed = timeutil.to_local_aware(trigger_time, user.timezone)
        row = await db.create_reminder(uid, parsed, message, checkin_for_task_id)
        _record("reminder", parsed)
        return f"Created reminder #{row['id']} for {timeutil.format_local(parsed, user.timezone)}: '{message}'."

    @tool
    async def cancel_reminder(reminder_id: int) -> str:
        """Cancel a pending reminder by id right away -- no confirmation needed."""
        row = await db.cancel_reminder(uid, reminder_id)
        return f"Cancelled reminder #{reminder_id}." if row else f"No pending reminder #{reminder_id} found."

    @tool
    async def add_note(content: str, tags: Optional[str] = None) -> str:
        """Save a new note for the user. Does not require confirmation."""
        row = await db.create_note(uid, content, tags)
        return f"Saved note #{row['id']}."

    @tool
    async def delete_note(note_id: int) -> str:
        """Delete a note by id right away -- no confirmation needed."""
        row = await db.delete_note(uid, note_id)
        return f"Deleted note #{note_id}." if row else f"No note #{note_id} found."

    @tool
    async def upsert_contact(
        name: str, relationship_type: Optional[str] = None, notes: Optional[str] = None
    ) -> str:
        """Create or update a contact by name. Does not require confirmation."""
        row = await db.upsert_person(uid, name, relationship_type, notes)
        return f"Saved contact '{row['name']}' (#{row['id']})."

    @tool
    async def delete_contact(name: str) -> str:
        """Delete a contact by name right away -- no confirmation needed."""
        row = await db.delete_person(uid, name)
        return f"Deleted contact '{name}'." if row else f"No contact named '{name}' found."

    # ---- gated: SCHEDULING only -- these stage the change, they don't apply it ----

    @tool
    async def propose_create_calendar_event(
        title: str, start_time: str, end_time: str,
        location: Optional[str] = None, notes: Optional[str] = None,
    ) -> str:
        """Propose a calendar event. start_time/end_time are interpreted in the
        user's own timezone if given without an explicit UTC offset. This does
        NOT create the event yet -- it stages it for the user's confirmation."""
        row = await db.propose_action(
            uid, "create_calendar_event",
            {
                "title": title, "start_time": start_time, "end_time": end_time,
                "location": location, "notes": notes, "user_timezone": user.timezone,
            },
        )
        return f"Proposed (pending confirmation, id #{row['id']}): event '{title}' at {start_time}."

    @tool
    async def propose_update_calendar_event(
        event_id: int,
        title: Optional[str] = None, start_time: Optional[str] = None, end_time: Optional[str] = None,
        location: Optional[str] = None, notes: Optional[str] = None, status: Optional[str] = None,
    ) -> str:
        """Propose updating a calendar event by id. Only non-null fields change."""
        row = await db.propose_action(
            uid, "update_calendar_event",
            {
                "event_id": event_id, "title": title, "start_time": start_time, "end_time": end_time,
                "location": location, "notes": notes, "status": status, "user_timezone": user.timezone,
            },
        )
        return f"Proposed (pending confirmation, id #{row['id']}): update event #{event_id}."

    @tool
    async def propose_delete_calendar_event(event_id: int) -> str:
        """Propose cancelling a calendar event by id."""
        row = await db.propose_action(uid, "delete_calendar_event", {"event_id": event_id})
        return f"Proposed (pending confirmation, id #{row['id']}): cancel event #{event_id}."

    raw_tools: list[BaseTool] = [
        list_tasks, list_reminders, list_calendar_events, list_notes, list_contacts,
        create_task, update_task, delete_task,
        create_reminder, cancel_reminder,
        add_note, delete_note,
        upsert_contact, delete_contact,
        propose_create_calendar_event, propose_update_calendar_event, propose_delete_calendar_event,
    ]
    return trace_all(raw_tools, LABEL)


def _build_system_prompt(user: UserContext) -> str:
    time_ctx = timeutil.current_context_str(user.timezone, user.timezone_confirmed)
    return (
        "You are the executive assistant specialist: tasks, reminders, notes, contacts, and "
        "calendar events, delegated to you by Messa.\n"
        f"{time_ctx}\n\n"
        "- Reads (list_*), tasks, reminders, notes, and contacts all happen IMMEDIATELY -- no "
        "confirmation needed for any of those.\n"
        "- Only calendar events (scheduling) require confirmation: propose_create_calendar_event, "
        "propose_update_calendar_event, and propose_delete_calendar_event only STAGE the change. "
        "After calling one, tell Messa exactly what was proposed and its pending id, so Messa can "
        "ask the user to confirm -- only Messa can actually confirm it.\n"
        "- Never claim a calendar event was created/changed/deleted unless you were told the "
        "proposal was confirmed. Tasks/reminders/notes/contacts you write directly ARE real the "
        "moment the tool call succeeds -- say so plainly.\n"
        "- Every date/time you pass to a tool is interpreted in the user's own timezone shown "
        "above, not UTC -- write times the way the user said them (e.g. '3pm tomorrow'), you "
        "don't need to do any timezone math yourself.\n"
        "- If the current date/time above is marked as an unconfirmed default timezone and this "
        "request involves a specific time, ask the user for their city or zip code before "
        "creating anything time-sensitive, rather than guessing.\n"
    )


def _past_due_issue(log: list[tuple[str, datetime]], user: UserContext) -> Optional[str]:
    """Deterministic post-hoc quality check -- no extra LLM call needed to
    run it. If anything just written this run landed more than a few
    minutes in the PAST relative to the user's real local now, that's
    almost always a parsing/timezone mistake rather than an intentional
    backdated entry, and life-event-adjacent mistakes here are exactly what
    this whole rework exists to catch. Mirrors the proven bounded
    retry-once shape from cli.py's run_turn stall-retry backstop."""
    if not log:
        return None
    tz = ZoneInfo(user.timezone)
    now_local = datetime.now(tz)
    problems = []
    for kind, when in log:
        if when is None:
            continue
        delta = now_local - when.astimezone(tz)
        if delta.total_seconds() > 300:  # more than 5 minutes in the past
            problems.append(
                f"a {kind} was just set for {timeutil.format_local(when, user.timezone)}, which is "
                f"already in the past (current time is {timeutil.format_local(now_local, user.timezone)})"
            )
    if not problems:
        return None
    return (
        "Before finishing, double check this: " + "; ".join(problems) + ". If that's a mistake "
        "(a misread date/time), fix it now with the appropriate update tool. If the user actually "
        "meant a time in the past on purpose, it's fine as-is -- just make sure that was intentional."
    )


def _last_ai_text(messages: list[Any]) -> str:
    for m in reversed(messages):
        content = getattr(m, "content", "")
        if getattr(m, "type", None) == "ai" and content:
            return content if isinstance(content, str) else str(content)
    return "Done."


def build_executive_subagent(user: UserContext, model: BaseChatModel) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec -- deliberately NOT built
    the same way as deepsearch's open-ended research loop. Personal-
    assistant work (a task, a reminder, a contact) is a handful of direct
    DB calls, not a multi-step browsing investigation: giving it
    deepsearch's ~100-step budget would just let a confused run wander for
    a long time before failing, when what this role actually needs is
    "quality and proper checking within small loops" (the explicit product
    ask).

    Two things make this small-loop-with-checking rather than just
    small-loop: (1) `config.EXECUTIVE_RECURSION_LIMIT` (default 14 -- room
    for several tool calls plus a summary, not room for a runaway loop),
    and (2) one deterministic, non-LLM quality check after the inner agent
    finishes (`_past_due_issue` above): if anything it just wrote landed
    suspiciously in the past for the user's real timezone, it gets exactly
    one nudge to look again and fix it before this delegation's reply goes
    back to Messa -- the same bounded retry-once shape already proven for
    the orchestrator's own dropped-delegation bug (see cli.py's run_turn).
    """

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state["messages"])
        written: list[tuple[str, datetime]] = []
        tools = build_executive_tools(user, written)

        checkpointer = MemorySaver()
        run_config = {
            "configurable": {"thread_id": f"executive-{user.user_id}"},
            "recursion_limit": config.EXECUTIVE_RECURSION_LIMIT,
        }
        inner_agent = create_agent(
            model=model, tools=tools, system_prompt=_build_system_prompt(user),
            checkpointer=checkpointer,
        )
        result = await inner_agent.ainvoke({"messages": messages}, config=run_config)
        final_messages = result["messages"]

        issue = _past_due_issue(written, user)
        if issue:
            written.clear()
            nudge = HumanMessage(content=f"(auto-check, not from the user: {issue})")
            result = await inner_agent.ainvoke({"messages": [nudge]}, config=run_config)
            final_messages = result["messages"]

        return {"messages": [AIMessage(content=_last_ai_text(final_messages))]}

    return {
        "name": "executive_assistant",
        "description": (
            "Manages tasks, reminders, notes, contacts, and calendar events. Use for "
            "anything about the user's to-dos, schedule, or personal notes/contacts. "
            "Tasks/reminders/notes/contacts happen immediately; calendar events "
            "(scheduling) go through a confirm step."
        ),
        "runnable": RunnableLambda(_run),
    }
