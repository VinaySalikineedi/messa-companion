"""V3-autonomous.md Phase 5, SENSE 5 -- "Meeting Dossiers": a T-10-minute
pre-meeting SMS brief (*"3 crisp bullets on who you're meeting, what you
discussed last, and what your objective is"*) and a T+3-minute post-meeting
prompt asking for a quick voice note (*"converts them into drafted
follow-ups, intros, and CRM notes"*).

Deterministic, template-rendered, no model call in the poll loop at all --
same reasoning as briefings.py's own module docstring: assembling a
handful of already-known facts (an event's title/time/location/notes, any
open commitment-ledger entries for the same counterparty) needs no
judgment, so there's no reason to pay for or wait on an LLM call across a
whole due batch. The genuinely generative step -- turning a voice memo
recorded AFTER the T+3 prompt into real drafted follow-ups/intros/CRM
notes -- happens naturally through Messa's existing inbound-voice-memo
path (media_understanding.py) plus a normal orchestrator turn; this module
only sends the prompt and leaves a short-lived breadcrumb (db.set_pending_
post_meeting_note) so agents/registry.py's system prompt recognizes the
reply that follows as meeting notes, not an ordinary message.

Calendar source, per-user, exactly as directed rather than a native-only
default: db.get_app_preference(uid, 'calendar') decides whether a given
user's events come from Messa's own calendar_events table (the 'messa'/
unset case -- get_due_native_pre_meeting_events/get_due_native_post_
meeting_events already do this filtering in one SQL query, joined with
users) or from a connected app via tools/integration_tools.py's
list_connected_calendar_events (db.list_users_with_connected_calendar_
primary finds who to check; see that function's own docstring for why a
connected user's connection can't be prefiltered by status and is instead
checked live, once per poll tick, same as every other Composio toolkit in
this codebase).

Both event sources are normalized into ONE shape before anything else in
this module touches them --
{event_key, title, start_time, end_time, location, notes, attendees} --
so render_pre_meeting_brief/render_post_meeting_prompt and the dedup
ledger (meeting_dossier_events, see db.py's own section comment) never
need to know or care which calendar an event actually came from.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import config, db
from .tools import integration_tools

# Short, generic words that show up in almost every meeting title
# ("meeting", "call", "sync", "with", ...) and would otherwise turn into
# noisy, near-universal ILIKE patterns against user_commitments.
# counterparty_name (see _title_hints below) -- deliberately small and
# hand-maintained, same "static beats a live classifier for a tiny, rarely
# -changing set" reasoning as integration_tools.py's own TOOLKIT_APP_
# CATEGORY.
_TITLE_STOPWORDS = {
    "the", "and", "with", "for", "about", "meeting", "meet", "call", "chat",
    "sync", "catch", "catchup", "check", "checkin", "review", "intro",
    "introduction", "discussion", "planning", "weekly", "monthly", "daily",
}


def _normalize_native_row(row: dict[str, Any]) -> dict[str, Any]:
    """calendar_events row -> this module's common event shape. Native
    events carry no structured attendee data (see neon-schema.sql's
    calendar_events definition -- title/start_time/end_time/location/notes
    only), so attendees is always []; see this module's own docstring on
    why the pre-meeting brief's content is scoped to what's actually
    available rather than a full relationship-radar lookup."""
    return {
        "event_key": f"native:{row['id']}",
        "title": row.get("title") or "Untitled event",
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "location": row.get("location"),
        "notes": row.get("notes"),
        "attendees": [],
    }


def _title_hints(title: str) -> list[str]:
    words = [w.strip(".,!?:;()[]\"'") for w in (title or "").split()]
    return [w for w in words if len(w) >= 3 and w.lower() not in _TITLE_STOPWORDS]


def _event_hints(event: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(name_hints, email_hints) fed to db.list_open_commitments_for_hints
    to answer "did the user promise this counterparty anything?" --
    attendee display names/emails when the calendar actually has them
    (connected calendars only), plus individual meaningful words from the
    event title either way (covers the common native-calendar case:
    "Coffee with Sarah Chen" -> hints ["Coffee", "Sarah", "Chen"] -- a
    commitment recorded with counterparty_name "Sarah Chen" (see
    commitments.py's _guess_counterparty_name) matches on "Sarah" or
    "Chen")."""
    name_hints = list(_title_hints(event.get("title") or ""))
    email_hints: list[str] = []
    for a in event.get("attendees") or []:
        if a.get("email"):
            email_hints.append(a["email"])
        if a.get("name"):
            name_hints.append(a["name"])
    return name_hints, email_hints


def _clock(dt: datetime, tz: ZoneInfo) -> str:
    """"9:00 AM" -- same no-leading-zero shape as briefings.py's own
    _clock (duplicated rather than imported, matching that module's own
    stated preference for zero cross-dependency between these two
    sibling "assemble known facts into an SMS" modules)."""
    local_dt = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(tz)
    text = local_dt.strftime("%I:%M %p")
    return text[1:] if text.startswith("0") else text


async def _render_pre_meeting_brief(user_id: int, event: dict[str, Any], tz_name: str) -> str:
    tz = ZoneInfo(tz_name)
    lines = [f"Heads up -- {_clock(event['start_time'], tz)}: {event['title']}."]

    name_hints, email_hints = _event_hints(event)
    if event.get("notes"):
        lines.append(f"Last notes: {event['notes'].strip()}")

    commitments = await db.list_open_commitments_for_hints(user_id, name_hints, email_hints)
    if commitments:
        top = commitments[0]
        lines.append(f"Open: you told them you'd {top['commitment_summary']}.")

    return " ".join(lines)


def _render_post_meeting_prompt(event: dict[str, Any]) -> str:
    return (
        f"How'd \"{event['title']}\" go? Send me a quick voice note and I'll turn it into "
        "follow-ups, intros, or notes for you."
    )


async def get_due_pre_meeting_briefs(now: datetime | None = None) -> list[dict[str, Any]]:
    """Everything due for a T-10-minute pre-meeting brief right now, across
    BOTH native and connected calendars, each item already carrying its
    rendered `text` -- server.py's poller just sends it to `phone_number`
    and calls db.mark_pre_brief_sent(user_id, event_key) once delivery
    succeeds. A no-op ([]) entirely when config.MEETING_DOSSIERS_ENABLED is
    off."""
    if not config.MEETING_DOSSIERS_ENABLED:
        return []
    now = now or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []

    for row in await db.get_due_native_pre_meeting_events(now):
        event = _normalize_native_row(row)
        tz_name = row.get("user_timezone") or config.DEFAULT_TIMEZONE
        items.append({
            "user_id": row["user_id"],
            "phone_number": row["phone_number"],
            "event_key": event["event_key"],
            "text": await _render_pre_meeting_brief(row["user_id"], event, tz_name),
            "event": event,
        })

    window_end = now + timedelta(minutes=config.PRE_MEETING_BRIEF_LEAD_MINUTES)
    for user_row in await db.list_users_with_connected_calendar_primary():
        user_ctx = config.UserContext(user_id=user_row["user_id"], phone_number=user_row["phone_number"])
        events = await integration_tools.list_connected_calendar_events(
            user_ctx, user_row["preferred_app"], now, window_end,
        )
        tz_name = user_row.get("user_timezone") or config.DEFAULT_TIMEZONE
        for event in events:
            if event["start_time"] <= now or event["start_time"] > window_end:
                continue
            if await db.is_dossier_event_already_sent(user_row["user_id"], event["event_key"], "pre_brief"):
                continue
            items.append({
                "user_id": user_row["user_id"],
                "phone_number": user_row["phone_number"],
                "event_key": event["event_key"],
                "text": await _render_pre_meeting_brief(user_row["user_id"], event, tz_name),
                "event": event,
            })
    return items


async def get_due_post_meeting_harvests(now: datetime | None = None) -> list[dict[str, Any]]:
    """The post-meeting counterpart to get_due_pre_meeting_briefs above:
    events that ended within the last config.POST_MEETING_HARVEST_DELAY_
    MINUTES, not yet harvested, across both calendar sources."""
    if not config.MEETING_DOSSIERS_ENABLED:
        return []
    now = now or datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []

    for row in await db.get_due_native_post_meeting_events(now):
        event = _normalize_native_row(row)
        items.append({
            "user_id": row["user_id"],
            "phone_number": row["phone_number"],
            "event_key": event["event_key"],
            "text": _render_post_meeting_prompt(event),
            "event": event,
        })

    window_start = now - timedelta(minutes=config.POST_MEETING_HARVEST_DELAY_MINUTES)
    for user_row in await db.list_users_with_connected_calendar_primary():
        user_ctx = config.UserContext(user_id=user_row["user_id"], phone_number=user_row["phone_number"])
        events = await integration_tools.list_connected_calendar_events(
            user_ctx, user_row["preferred_app"], window_start, now,
        )
        for event in events:
            if event["end_time"] > now or event["end_time"] <= window_start:
                continue
            if await db.is_dossier_event_already_sent(user_row["user_id"], event["event_key"], "post_harvest"):
                continue
            items.append({
                "user_id": user_row["user_id"],
                "phone_number": user_row["phone_number"],
                "event_key": event["event_key"],
                "text": _render_post_meeting_prompt(event),
                "event": event,
            })
    return items
