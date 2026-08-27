"""Executive assistant subagent: tasks, reminders, notes, contacts, calendar.

Design: every mutation type listed in the DB's `action_type` enum (create/
update/delete task, create/cancel reminder, create/update/delete calendar
event, delete note) goes through a `propose_*` tool that writes a row to
`pending_actions` and returns without touching the real table. Only Messa
(the orchestrator) can actually confirm it -- see agents/registry.py's
`confirm_pending_action` / `reject_pending_action` tools -- after the user
has explicitly said yes in conversation. This subagent is deliberately
unable to commit its own proposals.

Anything NOT in that enum (reads, creating a note, contacts) writes
directly since the schema's own design implies those don't need a
confirmation round-trip.
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import BaseTool, tool

from .. import db
from ..config import UserContext
from .common import trace_all

LABEL = "executive_assistant"


def build_executive_tools(user: UserContext) -> list[BaseTool]:
    uid = user.user_id

    # ---- reads ----

    @tool
    async def list_tasks(status: Optional[str] = None) -> str:
        """List the user's tasks. status: one of todo/in_progress/done/cancelled, or omit for all open tasks."""
        rows = await db.list_tasks(uid, status)
        if not rows:
            return "No tasks found."
        return "\n".join(
            f"#{r['id']} [{r['status']}/{r['priority']}] {r['title']}"
            + (f" (due {r['due_date']})" if r["due_date"] else "")
            for r in rows
        )

    @tool
    async def list_reminders(status: str = "pending") -> str:
        """List the user's reminders. status: pending/sent/cancelled."""
        rows = await db.list_reminders(uid, status)
        if not rows:
            return f"No {status} reminders."
        return "\n".join(f"#{r['id']} at {r['trigger_time']}: {r['message']}" for r in rows)

    @tool
    async def list_calendar_events(upcoming_only: bool = True) -> str:
        """List the user's calendar events."""
        rows = await db.list_calendar_events(uid, upcoming_only)
        if not rows:
            return "No events found."
        return "\n".join(
            f"#{r['id']} {r['title']}: {r['start_time']} - {r['end_time']}"
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

    # ---- direct writes (not in the confirmable action_type enum) ----

    @tool
    async def add_note(content: str, tags: Optional[str] = None) -> str:
        """Save a new note for the user. Does not require confirmation."""
        row = await db.create_note(uid, content, tags)
        return f"Saved note #{row['id']}."

    @tool
    async def upsert_contact(
        name: str, relationship_type: Optional[str] = None, notes: Optional[str] = None
    ) -> str:
        """Create or update a contact by name. Does not require confirmation."""
        row = await db.upsert_person(uid, name, relationship_type, notes)
        return f"Saved contact '{row['name']}' (#{row['id']})."

    # ---- gated proposals: these only stage the change, they do not apply it ----

    @tool
    async def propose_create_task(
        title: str,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> str:
        """Propose creating a task. due_date is an ISO-8601 timestamp if given.
        priority: low/medium/high/urgent. This does NOT create the task yet --
        it stages it for the user's confirmation. Tell the user what you're proposing
        and ask them to confirm before it's created."""
        row = await db.propose_action(
            uid, "create_task",
            {"title": title, "description": description, "due_date": due_date, "priority": priority},
        )
        return f"Proposed (pending confirmation, id #{row['id']}): create task '{title}'."

    @tool
    async def propose_update_task(
        task_id: int,
        title: Optional[str] = None,
        description: Optional[str] = None,
        due_date: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
    ) -> str:
        """Propose updating an existing task by id. Only non-null fields change."""
        row = await db.propose_action(
            uid, "update_task",
            {
                "task_id": task_id, "title": title, "description": description,
                "due_date": due_date, "status": status, "priority": priority,
            },
        )
        return f"Proposed (pending confirmation, id #{row['id']}): update task #{task_id}."

    @tool
    async def propose_delete_task(task_id: int) -> str:
        """Propose deleting a task by id."""
        row = await db.propose_action(uid, "delete_task", {"task_id": task_id})
        return f"Proposed (pending confirmation, id #{row['id']}): delete task #{task_id}."

    @tool
    async def propose_create_reminder(
        trigger_time: str, message: str, checkin_for_task_id: Optional[int] = None
    ) -> str:
        """Propose a reminder. trigger_time is an ISO-8601 timestamp."""
        row = await db.propose_action(
            uid, "create_reminder",
            {"trigger_time": trigger_time, "message": message, "checkin_for_task_id": checkin_for_task_id},
        )
        return f"Proposed (pending confirmation, id #{row['id']}): reminder '{message}' at {trigger_time}."

    @tool
    async def propose_cancel_reminder(reminder_id: int) -> str:
        """Propose cancelling a pending reminder by id."""
        row = await db.propose_action(uid, "cancel_reminder", {"reminder_id": reminder_id})
        return f"Proposed (pending confirmation, id #{row['id']}): cancel reminder #{reminder_id}."

    @tool
    async def propose_create_calendar_event(
        title: str, start_time: str, end_time: str,
        location: Optional[str] = None, notes: Optional[str] = None,
    ) -> str:
        """Propose a calendar event. start_time/end_time are ISO-8601 timestamps."""
        row = await db.propose_action(
            uid, "create_calendar_event",
            {"title": title, "start_time": start_time, "end_time": end_time, "location": location, "notes": notes},
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
                "location": location, "notes": notes, "status": status,
            },
        )
        return f"Proposed (pending confirmation, id #{row['id']}): update event #{event_id}."

    @tool
    async def propose_delete_calendar_event(event_id: int) -> str:
        """Propose cancelling a calendar event by id."""
        row = await db.propose_action(uid, "delete_calendar_event", {"event_id": event_id})
        return f"Proposed (pending confirmation, id #{row['id']}): cancel event #{event_id}."

    @tool
    async def propose_delete_note(note_id: int) -> str:
        """Propose deleting a note by id."""
        row = await db.propose_action(uid, "delete_note", {"note_id": note_id})
        return f"Proposed (pending confirmation, id #{row['id']}): delete note #{note_id}."

    raw_tools: list[BaseTool] = [
        list_tasks, list_reminders, list_calendar_events, list_notes, list_contacts,
        add_note, upsert_contact,
        propose_create_task, propose_update_task, propose_delete_task,
        propose_create_reminder, propose_cancel_reminder,
        propose_create_calendar_event, propose_update_calendar_event, propose_delete_calendar_event,
        propose_delete_note,
    ]
    return trace_all(raw_tools, LABEL)


EXECUTIVE_SYSTEM_PROMPT = (
    "You are the executive assistant specialist: tasks, reminders, notes, contacts, and "
    "calendar events, delegated to you by Messa.\n"
    "- Reads (list_*) and notes/contacts happen immediately -- no confirmation needed.\n"
    "- Creating, updating, or deleting a task/reminder/calendar-event, or deleting a note, "
    "goes through a propose_* tool. That only STAGES the change -- it is not applied yet.\n"
    "- After calling a propose_* tool, tell Messa exactly what was proposed and its pending "
    "id, so Messa can ask the user to confirm. Only Messa can actually confirm it.\n"
    "- Never claim something was created/changed/deleted unless it was a direct write "
    "(add_note, upsert_contact) or you were told the proposal was confirmed.\n"
)
