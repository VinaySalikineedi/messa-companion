"""Deterministic, template-rendered morning/evening briefings -- replaces
the earlier design where these were just another cron_jobs row whose saved
prompt_or_task got handed to a full Messa LLM turn (see config.py's
DEFAULT_BRIEFINGS, still the source of truth for cron_expression/kind/
provisioning; only HOW a due briefing job gets turned into text changes
here).

Why: a morning/evening briefing is calendar events + tasks due + reminders
+ weather -- all of it already sitting in Postgres or one weather API call
away, none of it requiring judgment or generation. Running it through an
LLM meant (a) server.py's _production_cron_loop handling every user's
briefing ONE AT A TIME, each a real model round-trip, so a busy send window
serialized behind however many users share a 7:00 AM cron tick, and (b)
paying for and waiting on a model call to produce text that was going to
say the same kind of thing every time anyway. This module fetches a single
user's data in parallel (asyncio.gather) and renders it with an f-string
template instead -- no model call in the loop at all -- and
server.py's new _production_briefing_loop fans that out across every due
user with its own asyncio.gather, so an entire batch of briefings goes out
concurrently rather than one user at a time.

Scope: this only ever handles the two system-provisioned kinds in
config.DEFAULT_BRIEFINGS ("morning_briefing"/"evening_briefing"). Any
OTHER recurring automation a user asks Messa to set up (routines_agent's
propose_create_recurring_cron) is arbitrary, model-authored text with no
fixed shape -- those still go through the original LLM path unchanged
(server.py's _production_cron_loop, now with these two kinds excluded so
they're not double-handled -- see get_due_briefing_jobs_for_delivery's own
docstring for that split).

Plain-text formatting throughout (no markdown, no bullets) -- same
SMS/iMessage convention agents/registry.py's own system prompt already
enforces for a live model turn ("no **bold**, no bullet/numbered lists...
the way a person texting would"); this is template text, not a model
turn, but it's read on the same channel and should look like it belongs
there.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import config, db, email_triage, weather


def _local_day_bounds_utc(tz_name: str, day_offset: int = 0) -> tuple[datetime, datetime]:
    """[local midnight, next local midnight) for "today" (day_offset=0) or
    "tomorrow" (day_offset=1), as a tz-aware UTC pair ready for
    db.list_calendar_events_for_range. Deliberately duplicated rather than
    imported from server.py's _week_bounds_utc (same three-line shape,
    just one day instead of seven) -- server.py already imports a great
    deal; this module intentionally has zero dependency on it so it can be
    unit-tested standalone and so server.py is the only thing that ever
    imports the other way around, never both."""
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(ZoneInfo("UTC")).astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=day_offset)
    next_midnight = local_midnight + timedelta(days=1)
    return local_midnight.astimezone(ZoneInfo("UTC")), next_midnight.astimezone(ZoneInfo("UTC"))


def _clock(dt: datetime | None, tz: ZoneInfo) -> str:
    """"9:00 AM" -- no leading zero (stripped manually, not via a libc
    "%-I" that isn't portable across platforms), same approach server.py's
    own _format_time_local already uses for the exact same reason."""
    if dt is None:
        return ""
    local_dt = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    text = local_dt.strftime("%I:%M %p")
    return text[1:] if text.startswith("0") else text


def _weekday_date(tz: ZoneInfo, day_offset: int = 0) -> str:
    """"Tue, Sep 1" for the greeting line."""
    local_now = (datetime.now(ZoneInfo("UTC")).astimezone(tz)) + timedelta(days=day_offset)
    return local_now.strftime("%a, %b %-d")


def _event_line(ev: dict[str, Any], tz: ZoneInfo) -> str:
    time_str = _clock(ev.get("start_time"), tz)
    title = ev.get("title") or "Untitled event"
    return f"{time_str} {title}".strip()


def _task_line(t: dict[str, Any], tz: ZoneInfo, *, today_local_date) -> str:
    title = t.get("title") or "Untitled task"
    due = t.get("due_date")
    if due is not None:
        due_local = due.astimezone(tz) if due.tzinfo else due.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        if due_local.date() < today_local_date:
            return f"{title} (overdue)"
    return title


def _reminder_line(r: dict[str, Any], tz: ZoneInfo) -> str:
    time_str = _clock(r.get("trigger_time"), tz)
    message = r.get("message") or ""
    return f"{time_str} {message}".strip()


async def _gather_day_data(user_id: int, tz_name: str, day_offset: int) -> dict[str, Any]:
    """Everything needed to describe ONE local day (today or tomorrow):
    that day's calendar events and any pending reminders due that day.
    Fetched together (asyncio.gather) since neither depends on the other."""
    start_utc, end_utc = _local_day_bounds_utc(tz_name, day_offset)
    events, reminders = await asyncio.gather(
        db.list_calendar_events_for_range(user_id, start_utc, end_utc),
        db.list_reminders(user_id, status="pending"),
    )
    tz = ZoneInfo(tz_name)
    day_reminders = []
    for r in reminders:
        trigger = r.get("trigger_time")
        if trigger is None:
            continue
        trigger_local = trigger.astimezone(tz) if trigger.tzinfo else trigger.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        if start_utc.astimezone(tz).date() == trigger_local.date():
            day_reminders.append(r)
    return {"events": events, "reminders": day_reminders}


async def _gather_weather(latitude: float | None, longitude: float | None, tz_name: str) -> list | None:
    if latitude is None or longitude is None:
        return None
    return await weather.get_daily_weather(latitude, longitude, tz_name)


def _greeting(user_name: str | None, label: str) -> str:
    return f"{label}, {user_name}!" if user_name else f"{label}!"


async def render_morning_briefing(job: dict[str, Any]) -> str:
    """`job` is one row from db.get_due_briefing_jobs_for_delivery --
    carries user_id, user_timezone (the cron row's own tz snapshot, kept
    in sync by db.ensure_default_briefings), user_name, latitude,
    longitude. Fetches everything for TODAY in parallel and renders a
    short, warm, plain-text briefing -- never raises: any single piece
    (weather, in particular) that fails or is unavailable is just dropped
    from the text rather than blocking or blanking the whole message."""
    user_id = job["user_id"]
    tz_name = job.get("user_timezone") or config.DEFAULT_TIMEZONE
    tz = ZoneInfo(tz_name)

    today_data, tasks, weather_days = await asyncio.gather(
        _gather_day_data(user_id, tz_name, day_offset=0),
        db.list_tasks(user_id),
        _gather_weather(job.get("latitude"), job.get("longitude"), tz_name),
    )

    today_local_date = datetime.now(ZoneInfo("UTC")).astimezone(tz).date()
    due_today_or_overdue = [
        t for t in tasks
        if t.get("due_date") is not None
        and (t["due_date"].astimezone(tz) if t["due_date"].tzinfo
             else t["due_date"].replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)).date() <= today_local_date
    ]

    lines = [f"{_greeting(job.get('user_name'), 'Morning')} {_weekday_date(tz)}."]

    if weather_days:
        lines.append(f"Weather: {weather_days[0].one_liner()}.")

    events = sorted(today_data["events"], key=lambda e: e.get("start_time") or datetime.max.replace(tzinfo=ZoneInfo("UTC")))
    if events:
        lines.append("Today: " + ", ".join(_event_line(e, tz) for e in events) + ".")

    if due_today_or_overdue:
        lines.append(
            "Due today: " + ", ".join(_task_line(t, tz, today_local_date=today_local_date) for t in due_today_or_overdue) + "."
        )

    reminders = sorted(today_data["reminders"], key=lambda r: r.get("trigger_time") or datetime.max.replace(tzinfo=ZoneInfo("UTC")))
    if reminders:
        lines.append("Reminders: " + ", ".join(_reminder_line(r, tz) for r in reminders) + ".")

    if not events and not due_today_or_overdue and not reminders:
        lines.append("Nothing on deck today -- let me know if you want to add anything or want me to look into something for you.")

    # V3-autonomous.md Phase 3: updates/receipts triaged out of an instant
    # per-email SMS during the day (messa/email_triage.py) roll in here
    # instead -- this is the actual fix for the "5:51 AM notification
    # fatigue" incident, not a separate text.
    digest_line = await email_triage.render_digest_section(
        user_id, email_triage.tiers_for_briefing_kind("morning_briefing", datetime.now(tz))
    )
    if digest_line:
        lines.append(digest_line)

    return "\n".join(lines)


async def render_evening_briefing(job: dict[str, Any]) -> str:
    """Recaps today (tasks completed, events that happened) and previews
    tomorrow (tomorrow's weather/events/reminders, plus anything due
    tomorrow or still overdue) -- same data-gathering/never-raises shape
    as render_morning_briefing above."""
    user_id = job["user_id"]
    tz_name = job.get("user_timezone") or config.DEFAULT_TIMEZONE
    tz = ZoneInfo(tz_name)

    today_start_utc, today_end_utc = _local_day_bounds_utc(tz_name, day_offset=0)

    today_data, tomorrow_data, tasks, done_tasks, weather_days = await asyncio.gather(
        _gather_day_data(user_id, tz_name, day_offset=0),
        _gather_day_data(user_id, tz_name, day_offset=1),
        db.list_tasks(user_id),
        db.list_tasks(user_id, status="done"),
        _gather_weather(job.get("latitude"), job.get("longitude"), tz_name),
    )

    tomorrow_local_date = (datetime.now(ZoneInfo("UTC")).astimezone(tz) + timedelta(days=1)).date()

    completed_today = [
        t for t in done_tasks
        if t.get("updated_at") is not None
        and today_start_utc <= (t["updated_at"] if t["updated_at"].tzinfo else t["updated_at"].replace(tzinfo=ZoneInfo("UTC"))) < today_end_utc
    ]
    due_tomorrow_or_overdue = [
        t for t in tasks
        if t.get("due_date") is not None
        and (t["due_date"].astimezone(tz) if t["due_date"].tzinfo
             else t["due_date"].replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)).date() <= tomorrow_local_date
    ]

    lines = [f"{_greeting(job.get('user_name'), 'Evening')} Wrapping up {_weekday_date(tz)}."]

    today_events = sorted(today_data["events"], key=lambda e: e.get("start_time") or datetime.max.replace(tzinfo=ZoneInfo("UTC")))
    today_bits = []
    if completed_today:
        today_bits.append("completed " + ", ".join(t.get("title") or "Untitled task" for t in completed_today))
    if today_events:
        today_bits.append("had " + ", ".join(_event_line(e, tz) for e in today_events))
    if today_bits:
        lines.append("Today: " + "; ".join(today_bits) + ".")

    # Schedule content and weather are tracked separately -- weather is
    # still worth showing on an otherwise-empty tomorrow (it's useful
    # information on its own), but it must NOT count as "there's something
    # going on" for the quiet-day fallback below. Without this split, a
    # user with nothing planned but a valid weather reading would never
    # see the "nothing on the calendar" nudge at all.
    tomorrow_schedule_bits = []
    tomorrow_events = sorted(tomorrow_data["events"], key=lambda e: e.get("start_time") or datetime.max.replace(tzinfo=ZoneInfo("UTC")))
    if tomorrow_events:
        tomorrow_schedule_bits.append(", ".join(_event_line(e, tz) for e in tomorrow_events))
    if due_tomorrow_or_overdue:
        tomorrow_schedule_bits.append("due: " + ", ".join(_task_line(t, tz, today_local_date=tomorrow_local_date) for t in due_tomorrow_or_overdue))

    tomorrow_bits = []
    if weather_days and len(weather_days) > 1:
        tomorrow_bits.append(weather_days[1].one_liner())
    tomorrow_bits.extend(tomorrow_schedule_bits)
    if tomorrow_bits:
        lines.append("Tomorrow: " + ". ".join(tomorrow_bits) + ".")

    tomorrow_reminders = sorted(tomorrow_data["reminders"], key=lambda r: r.get("trigger_time") or datetime.max.replace(tzinfo=ZoneInfo("UTC")))
    if tomorrow_reminders:
        lines.append("Reminders tomorrow: " + ", ".join(_reminder_line(r, tz) for r in tomorrow_reminders) + ".")

    if not today_bits and not tomorrow_schedule_bits and not tomorrow_reminders:
        lines.append("Quiet day -- nothing done, nothing on the calendar. Let me know if you want help planning tomorrow.")

    # Check for pending actions to ask if they can be marked off or kept
    try:
        pending_actions = await db.list_pending_actions(user_id)
        if pending_actions:
            items_str = ", ".join(f"{a.get('action_type', 'item')} (#{a.get('id')})" for a in pending_actions[:3])
            lines.append(f"Pending reviews: {len(pending_actions)} item(s) awaiting approval ({items_str}). Want me to mark these off or keep them?")
    except Exception:
        pass

    # V3-autonomous.md Phase 3: on a local Sunday only, this same daily 8pm
    # evening_briefing also carries the week's accumulated marketing/promo
    # digest (messa/email_triage.py) -- reusing this existing cron/kind as
    # the weekly digest's delivery slot rather than a new one. Any other
    # day of the week, tiers_for_briefing_kind returns [] here and this is
    # a no-op.
    digest_line = await email_triage.render_digest_section(
        user_id, email_triage.tiers_for_briefing_kind("evening_briefing", datetime.now(tz))
    )
    if digest_line:
        lines.append(digest_line)

    return "\n".join(lines)


_RENDERERS = {
    "morning_briefing": render_morning_briefing,
    "evening_briefing": render_evening_briefing,
}


async def render_briefing(job: dict[str, Any]) -> str | None:
    """Dispatches on job["kind"] -- returns None for a kind this module
    doesn't know how to render (shouldn't happen: server.py only ever
    calls this with rows db.get_due_briefing_jobs_for_delivery already
    filtered to config.DEFAULT_BRIEFINGS' own keys), so a caller can treat
    None as "skip, don't send anything" rather than crash on an
    unrecognized kind."""
    renderer = _RENDERERS.get(job.get("kind"))
    if renderer is None:
        return None
    return await renderer(job)
