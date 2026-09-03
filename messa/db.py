"""Async data access layer over the Neon Postgres database.

One asyncpg pool, thin repository functions grouped by table. Nothing here
knows about LangChain/agents — tools in `messa/tools/*` call these functions
and translate results into tool-call strings.

`payload` columns are TEXT (JSON-serialized), matching the schema exactly
(not JSONB), so we json.dumps/json.loads by hand at the boundary.
"""
from __future__ import annotations

import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import asyncpg
from croniter import croniter

from . import config, console, timeutil

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        # statement_cache_size=0: your DATABASE_URL points at Neon's pooled
        # endpoint (the "-pooler" host), which is PgBouncer in transaction-
        # pooling mode -- a single asyncpg "connection" can be multiplexed
        # across different physical Postgres backends between queries.
        # asyncpg's default behavior is to server-side-prepare and cache
        # each unique query per connection; with PgBouncer in the middle
        # (and especially right after a migration's DDL changes a table's
        # schema underneath an already-open connection), that cached plan
        # can point at a backend/catalog state that no longer matches,
        # which is exactly the "cached statement plan is invalid due to a
        # database schema or configuration change" error you hit. This is
        # a well-known asyncpg+PgBouncer incompatibility (Neon's own docs
        # recommend the same fix) -- disabling the cache costs a small
        # amount of per-query overhead in exchange for never hitting this
        # again, which matters a lot here since this project's whole
        # migration philosophy is additive ALTER TABLEs run against the
        # live database while the app keeps running.
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0,
        )
        async with _pool.acquire() as conn:
            try:
                await conn.execute("ALTER TABLE pending_actions ALTER COLUMN action_type TYPE VARCHAR(100) USING action_type::text;")
            except Exception:  # noqa: BLE001
                pass
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _row(r: asyncpg.Record | None) -> dict[str, Any] | None:
    return dict(r) if r is not None else None


def _rows(rs: list[asyncpg.Record]) -> list[dict[str, Any]]:
    return [dict(r) for r in rs]


def _parse_dt(value: Any, user_tz: str | None = None) -> datetime | None:
    """asyncpg needs real datetime objects for timestamptz columns -- it won't
    parse ISO strings itself (unlike psycopg2). Tool-proposed payloads carry
    ISO-8601-ish strings (that's what the LLM produces and what JSON can
    store), so every applier that touches a timestamptz column runs values
    through this first.

    This is now a thin wrapper over timeutil.to_local_aware: when a caller
    knows the user's timezone (every real caller in this file does, via a
    "user_timezone" key in the payload -- see the tasks/reminders/calendar
    sections below), a value with no explicit UTC offset is interpreted as
    that user's LOCAL time, not UTC. This is the write-side fix for the
    "the LLM works in UTC" bug: a naive "3pm tomorrow" from the model used
    to be silently stored as 3pm UTC regardless of where the user actually
    lives. `user_tz` defaults to UTC only for the rare internal caller that
    doesn't have a real user timezone to hand (there should be none left
    after this change, but this keeps old behavior rather than raising)."""
    return timeutil.to_local_aware(value, user_tz or "UTC")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

ONBOARDING_STEPS = [
    "awaiting_name", "awaiting_location", "awaiting_email", "complete",
]
# Order is name -> city/zip -> email (deliberately in this order, not the
# original name -> email -> city): location is the one onboarding answer
# that's actually load-bearing (it drives timezone-correct scheduling/
# reminders/weather), so it comes right after name rather than after the
# skippable email question. There used to be a 4th step here,
# "awaiting_email_connect" (an explicit "want me to connect your Gmail?"
# yes/no gate before onboarding could reach "complete") -- removed by
# request: it added a full extra back-and-forth before the user ever saw
# what Messa actually is (see cli.py's onboarding-complete reveal message,
# sent the moment this reaches "complete"), for a capability Messa can
# already offer conversationally any time ("connect my gmail") -- see
# agents/registry.py's `known_str` ("Gmail is NOT connected yet ... can
# send them a connect link on request"), which was true before this change
# and still is. Nothing about actually connecting Gmail changed -- only
# that it's no longer a gate onboarding has to pass through first.


def _initial_onboarding_step(name: str | None) -> str:
    """Where a brand-new user's onboarding starts, given what we already know."""
    return "awaiting_location" if name else "awaiting_name"


async def get_or_create_user(
    phone_number: str,
    name: str | None = None,
    timezone_name: str = config.DEFAULT_TIMEZONE,
) -> dict[str, Any]:
    """Look up (or create) the user for this phone number.

    A new user starts at 'awaiting_name' (the schema's own default) unless a
    name was already supplied -- Messa's system prompt uses onboarding_step
    to decide whether/what to casually ask for at the start of the
    conversation (see agents/registry.py). Existing users are returned as-is;
    onboarding only runs once.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE phone_number = $1", phone_number)
        if row:
            return dict(row)
        row = await conn.fetchrow(
            """
            INSERT INTO users (phone_number, name, timezone, onboarding_step)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            phone_number,
            name,
            timezone_name,
            _initial_onboarding_step(name),
        )
        return dict(row)


async def get_user_by_id(user_id: int) -> dict[str, Any] | None:
    """Straight id lookup -- used wherever a user is already resolved by
    something other than their phone number (e.g. cli.load_user_context_by_id,
    for a turn triggered by an inbound personal email rather than an inbound
    text -- see get_user_by_messa_email_local_part below)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        return dict(row) if row else None


async def update_user_timezone(
    user_id: int,
    timezone_name: str,
    confirmed: bool = True,
    latitude: float | None = None,
    longitude: float | None = None,
) -> dict[str, Any] | None:
    """Overwrite a user's stored timezone -- the one place, other than
    account creation, that users.timezone is ever written. `confirmed`
    tracks whether this came from an actually-resolved city/zip (see
    timeutil.resolve_timezone) as opposed to still being the
    config.DEFAULT_TIMEZONE placeholder. Additive/no-op-safe: if
    migrations/007_timezone_confirmed.sql hasn't been applied yet, still
    updates the timezone itself, just without the confirmed flag.

    `latitude`/`longitude` (migrations/019_user_coordinates.sql, optional):
    the SAME resolution's coordinates, written in this one UPDATE alongside
    the timezone rather than a separate call -- both callers below always
    have both at once (they come from the same timeutil.resolve_timezone
    result). Guarded by its own _has_column check, independent of the
    timezone_confirmed one, so this degrades gracefully on a deployment
    that has 007 but not yet 019: the timezone still gets updated, just
    without coordinates. None (the default) leaves latitude/longitude
    untouched -- only ensure_timezone_resolved/save_profile_field's city
    branch ever pass real values here."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        has_confirmed_col = await _has_column(conn, "users", "timezone_confirmed")
        has_coords_col = latitude is not None and longitude is not None and await _has_column(
            conn, "users", "latitude"
        )
        if has_coords_col and has_confirmed_col:
            row = await conn.fetchrow(
                "UPDATE users SET timezone = $2, timezone_confirmed = $3, latitude = $4, longitude = $5 "
                "WHERE id = $1 RETURNING *",
                user_id, timezone_name, confirmed, latitude, longitude,
            )
        elif has_coords_col:
            row = await conn.fetchrow(
                "UPDATE users SET timezone = $2, latitude = $3, longitude = $4 WHERE id = $1 RETURNING *",
                user_id, timezone_name, latitude, longitude,
            )
        elif has_confirmed_col:
            row = await conn.fetchrow(
                "UPDATE users SET timezone = $2, timezone_confirmed = $3 WHERE id = $1 RETURNING *",
                user_id, timezone_name, confirmed,
            )
        else:
            row = await conn.fetchrow(
                "UPDATE users SET timezone = $2 WHERE id = $1 RETURNING *",
                user_id, timezone_name,
            )
        return dict(row) if row else None


async def ensure_timezone_resolved(user_row: dict[str, Any]) -> dict[str, Any]:
    """One-time backfill / catch-up, called on every user-context load (see
    cli.load_user_context): if this user has a city on file but their
    timezone was never actually confirmed against it -- an account created
    before this feature existed, or an earlier resolution attempt that
    failed or was never tried -- try resolving it again now. No-ops (and
    returns user_row unchanged) once timezone_confirmed is true, or if
    there's no city to resolve from yet, so this is cheap for the common
    case and only does real work for the accounts that actually need it."""
    if user_row.get("timezone_confirmed") or not user_row.get("city"):
        return user_row
    resolution = await timeutil.resolve_timezone(user_row["city"])
    if resolution is None:
        return user_row
    updated = await update_user_timezone(
        user_row["id"], resolution.timezone, confirmed=resolution.confident,
        latitude=resolution.latitude, longitude=resolution.longitude,
    )
    return updated or user_row


async def get_browserbase_context_id(user_id: int) -> str | None:
    """One Browserbase Context per user, created once and reused forever --
    see migrations/005_browserbase.sql. Additive/no-op-safe: returns None
    if migration 005 hasn't been applied yet (deepsearch_tools.py then just
    creates an unpersisted context for that one run)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "browserbase_context_id"):
            return None
        return await conn.fetchval(
            "SELECT browserbase_context_id FROM users WHERE id = $1", user_id
        )


async def save_browserbase_context_id(user_id: int, context_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "browserbase_context_id"):
            return
        await conn.execute(
            "UPDATE users SET browserbase_context_id = $2 WHERE id = $1", user_id, context_id
        )


# How long a live_view_started_at is trusted without being refreshed
# before the share page treats the session as stale and shows idle anyway
# -- a safety net for the one case the try/finally in deepsearch_tools.py
# can't cover: the whole process getting killed (OOM, host restart) mid-
# delegation, which would otherwise leave a user's page stuck showing
# "live" forever for a browser that's long gone.
LIVE_VIEW_STALE_AFTER = timedelta(minutes=15)


async def get_or_create_live_share_token(user_id: int) -> str | None:
    """Permanent, unguessable per-user link (see migrations/006_live_view.sql)
    -- generated once, reused forever, so it only needs to be texted to the
    user a single time even though Messa mentions it on every deepsearch
    delegation. Additive-safe: returns None if migration 006 hasn't been
    applied yet, so callers (the system prompt) just skip mentioning a link
    rather than erroring or sending a dead one."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "live_share_token"):
            return None
        existing = await conn.fetchval("SELECT live_share_token FROM users WHERE id = $1", user_id)
        if existing:
            return existing
        token = secrets.token_urlsafe(24)
        await conn.execute("UPDATE users SET live_share_token = $2 WHERE id = $1", user_id, token)
        return token


async def set_live_browser_active(user_id: int, live_view_url: str, task: str | None) -> None:
    """Marks this user as having a browser open right now -- see
    migrations/006_live_view.sql for why this is separate from
    deepsearch_sessions. Additive/no-op-safe."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "live_view_url"):
            return
        await conn.execute(
            """
            UPDATE users SET live_view_url = $2, live_view_task = $3, live_view_started_at = NOW()
            WHERE id = $1
            """,
            user_id, live_view_url, task,
        )


async def clear_live_browser_active(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "live_view_url"):
            return
        await conn.execute(
            "UPDATE users SET live_view_url = NULL, live_view_task = NULL, live_view_started_at = NULL "
            "WHERE id = $1",
            user_id,
        )


async def get_live_status_by_token(token: str) -> dict[str, Any] | None:
    """Looks up a user by their live-share token for the public /live/<token>
    page. Returns None for an unknown/bad token (the route renders a plain
    404 for that -- distinct from a *valid* token with nothing running,
    which returns {"active": False, ...} so a wrong link never quietly
    looks like a normal idle state)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "live_share_token"):
            return None
        row = await conn.fetchrow(
            "SELECT id, live_view_url, live_view_task, live_view_started_at, name "
            "FROM users WHERE live_share_token = $1",
            token,
        )
        if not row:
            return None
        started_at = row["live_view_started_at"]
        is_stale = (
            started_at is not None
            and datetime.now(timezone.utc) - started_at > LIVE_VIEW_STALE_AFTER
        )
        active = bool(row["live_view_url"]) and not is_stale
        return {
            "active": active,
            "live_view_url": row["live_view_url"] if active else None,
            "task": row["live_view_task"] if active else None,
            "name": row["name"],
            # Internal only -- server.py uses this to look up
            # live_activity.py's in-memory description/chain-of-thought log
            # for this user, then strips it before the JSON response goes
            # out (a raw internal id has no business in a public payload).
            "user_id": row["id"],
        }


async def get_user_by_live_token(token: str) -> dict[str, Any] | None:
    """Resolves a live-share token straight to the user's own id/name/
    timezone -- for the /live/<token>/dashboard route (see server.py),
    which shows a user's tasks/reminders/schedule/projects/contacts
    regardless of whether a browser is currently active. Deliberately
    separate from get_live_status_by_token above, which is scoped to
    browsing state (active/live_view_url/task) and used by a different
    route (/live/<token>/status) -- keeping them separate means neither
    caller has to reason about fields it doesn't need. Returns None for an
    unknown token, or if migration 006 (which added live_share_token)
    hasn't been applied -- same "no dead link looks like a valid empty
    state" reasoning as get_live_status_by_token."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "live_share_token"):
            return None
        row = await conn.fetchrow(
            "SELECT id, name, timezone FROM users WHERE live_share_token = $1",
            token,
        )
        return dict(row) if row else None


async def list_calendar_events_for_range(user_id: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """All non-cancelled events whose start_time falls within [start, end)
    -- for the live-view dashboard's weekly schedule tile. Unlike
    list_calendar_events (which only ever shows events from now onward),
    this intentionally also includes events EARLIER in the requested range
    than the current moment, so a Monday-morning event still shows up in
    "this week" on a Wednesday. `start`/`end` are expected to be tz-aware
    UTC datetimes -- server.py computes them from the user's own local
    timezone (week boundaries mean something different in each zone)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM calendar_events WHERE user_id = $1 AND status = 'scheduled' "
            "AND start_time >= $2 AND start_time < $3 ORDER BY start_time",
            user_id, start, end,
        )
        return _rows(rows)


async def save_profile_field(user_id: int, field: str, value: str) -> dict[str, Any]:
    """Save one onboarding field (name/city/email) and advance onboarding_step.

    `value` may be the literal string 'skip' for the optional email step --
    still advances onboarding without writing anything (Messa's own address
    covers signups/accounts either way, see cli.py's onboarding-complete
    reveal message).

    For `field == "city"`, this also resolves and stores the user's real
    timezone (via timeutil.resolve_timezone) instead of leaving it on
    whatever config.DEFAULT_TIMEZONE was written at account creation -- the
    root fix for a user's stored timezone never actually reflecting where
    they live. The returned dict's `timezone_confirmed` tells the caller
    (agents/registry.py's save_profile_info tool) whether that resolution
    was unambiguous; when it's False, the caller should ask the user for a
    zip code or "city, state" instead of trusting a guess.

    Connecting Gmail is NOT one of these fields (there used to be a fourth
    'connect_email' field/onboarding step for that -- removed, see
    ONBOARDING_STEPS' comment above): it's handled entirely conversationally
    now, any time the user asks, via email_agent's request_email_connection
    tool -- no onboarding_step bookkeeping involved.
    """
    if field not in ("name", "email", "city"):
        raise ValueError(f"Unknown profile field: {field}")

    pool = await get_pool()
    async with pool.acquire() as conn:
        if field == "email" and not await _has_column(conn, "users", "email"):
            # Migration 003 not applied yet -- skip storing, still advance.
            pass
        elif value and value.strip().lower() != "skip":
            await conn.execute(f"UPDATE users SET {field} = $2 WHERE id = $1", user_id, value.strip())

        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
        current_step = row["onboarding_step"]
        step_for_field = {
            "name": "awaiting_name",
            "city": "awaiting_location",
            "email": "awaiting_email",
        }[field]
        if current_step == step_for_field:
            next_step = ONBOARDING_STEPS[ONBOARDING_STEPS.index(step_for_field) + 1]
            await conn.execute("UPDATE users SET onboarding_step = $2 WHERE id = $1", user_id, next_step)
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

    if field == "city" and value and value.strip().lower() != "skip":
        resolution = await timeutil.resolve_timezone(value.strip())
        if resolution is not None:
            updated = await update_user_timezone(
                user_id, resolution.timezone, confirmed=resolution.confident,
                latitude=resolution.latitude, longitude=resolution.longitude,
            )
            if updated:
                row = updated
    return dict(row)


# ---------------------------------------------------------------------------
# Message history
# ---------------------------------------------------------------------------

async def append_message(user_id: int, role: str, content: str, channel: str = "cli") -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        has_channel = await _has_column(conn, "message_history", "channel")
        if has_channel:
            row = await conn.fetchrow(
                """
                INSERT INTO message_history (user_id, role, content, channel)
                VALUES ($1, $2, $3, $4) RETURNING id
                """,
                user_id, role, content, channel,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO message_history (user_id, role, content)
                VALUES ($1, $2, $3) RETURNING id
                """,
                user_id, role, content,
            )
        return row["id"]


async def get_recent_messages(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM message_history WHERE user_id = $1
            ORDER BY timestamp DESC LIMIT $2
            """,
            user_id, limit,
        )
        return list(reversed(_rows(rows)))


_column_cache: dict[tuple[str, str], bool] = {}


async def _has_column(conn: asyncpg.Connection, table: str, column: str) -> bool:
    """True is cached forever (an additive migration never removes a
    column, so that answer can't go stale); False is deliberately NEVER
    cached, and re-checked on every call instead.

    This asymmetry matters: this whole codebase's migration philosophy is
    "ship the code, run the migration against the live Neon DB separately"
    -- meaning there's a real window where the running process's first
    call here happens *before* you've run that turn's migration. The
    original version cached whatever it saw first, including False,
    forever, in a plain in-memory dict with no invalidation -- so a check
    that ran once before migrations/006_live_view.sql landed would keep
    reporting "column doesn't exist" for the rest of that process's
    lifetime even seconds after the migration actually succeeded. That's
    exactly what caused live-view links to never generate and every
    /live/<token>/status lookup to 404 even for a token that genuinely
    existed in the users table -- the app's own belief about the schema
    was stuck in the past until the next restart. Re-checking on every
    False is one cheap information_schema query in the (should be brief)
    window before a migration has run, and free forever after."""
    key = (table, column)
    if _column_cache.get(key):
        return True
    exists = await conn.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
        )
        """,
        table, column,
    )
    if exists:
        _column_cache[key] = True
    return bool(exists)


# ---------------------------------------------------------------------------
# Tasks -- direct reads AND writes. Per explicit product decision, only
# SCHEDULING (calendar events, see below) requires confirmation; tasks are
# low-stakes and trivially reversible with a follow-up message, so they
# write immediately. `due_date`, when given, must already be a tz-aware
# datetime -- callers (tools/executive_tools.py) resolve it via
# timeutil.to_local_aware(raw, user.timezone) before calling these.
# ---------------------------------------------------------------------------

async def list_tasks(user_id: int, status: str | None = None) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if status:
            rows = await conn.fetch(
                "SELECT * FROM tasks WHERE user_id = $1 AND status = $2 ORDER BY due_date NULLS LAST, priority DESC",
                user_id, status,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM tasks WHERE user_id = $1 AND status != 'done' ORDER BY due_date NULLS LAST, priority DESC",
                user_id,
            )
        return _rows(rows)


async def create_task(
    user_id: int,
    title: str,
    description: str | None = None,
    due_date: datetime | None = None,
    priority: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    pool = await get_pool()
    status_val = status or "todo"
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tasks (user_id, title, description, due_date, priority, status)
            VALUES ($1, $2, $3, $4, COALESCE($5::task_priority, 'medium'), $6::task_status)
            RETURNING *
            """,
            user_id, title, description, due_date, priority, status_val,
        )
        return dict(row)


async def update_task(
    user_id: int,
    task_id: int,
    title: str | None = None,
    description: str | None = None,
    due_date: datetime | None = None,
    status: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE tasks SET
                title = COALESCE($3, title),
                description = COALESCE($4, description),
                due_date = COALESCE($5, due_date),
                status = COALESCE($6::task_status, status),
                priority = COALESCE($7::task_priority, priority),
                updated_at = NOW()
            WHERE id = $1 AND user_id = $2
            RETURNING *
            """,
            task_id, user_id, title, description, due_date, status, priority,
        )
        return dict(row) if row else {}


async def delete_task(user_id: int, task_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM tasks WHERE id = $1 AND user_id = $2 RETURNING id",
            task_id, user_id,
        )
        return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

async def list_reminders(user_id: int, status: str = "pending") -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM reminders WHERE user_id = $1 AND status = $2 ORDER BY trigger_time",
            user_id, status,
        )
        return _rows(rows)


async def get_due_reminders(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM reminders WHERE status = 'pending' AND trigger_time <= $1",
            now,
        )
        return _rows(rows)


async def get_due_reminders_for_delivery(now: datetime | None = None) -> list[dict[str, Any]]:
    """Same as get_due_reminders, but joined with users for the phone_number
    a production sender needs -- used by server.py's real (Sendblue-backed)
    reminder poller, as opposed to the CLI's local console-only preview."""
    now = now or datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT r.*, u.phone_number, u.timezone AS user_timezone
            FROM reminders r JOIN users u ON u.id = r.user_id
            WHERE r.status = 'pending' AND r.trigger_time <= $1
            """,
            now,
        )
        return _rows(rows)


async def mark_reminder_sent(reminder_id: int) -> None:
    """Called once a reminder has actually been delivered to the user (see
    server.py's _production_reminder_loop and background.py's CLI-only
    preview poller). Deletes the row outright rather than merely flipping
    its status to 'sent' (the previous behavior) -- explicit product
    decision: a reminder that already did its job (told the user something,
    at the time they needed to hear it) has no further use sitting around,
    and leaving it behind read as confusing clutter rather than history.
    reminder_status's 'sent' enum value is left defined (an enum value is
    never dropped once shipped) but nothing writes it anymore -- delete
    captures "this fired" more directly than a status a UI still has to
    know to filter out. A reminder that's somehow already gone by the time
    this runs is a silent no-op, same as every other id-keyed delete in
    this file (e.g. delete_task) -- not an error worth surfacing."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM reminders WHERE id = $1", reminder_id)


async def create_reminder(
    user_id: int,
    trigger_time: datetime,
    message: str,
    checkin_for_task_id: int | None = None,
) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO reminders (user_id, trigger_time, message, status, checkin_for_task_id)
            VALUES ($1, $2, $3, 'pending'::reminder_status, $4) RETURNING *
            """,
            user_id, trigger_time, message, checkin_for_task_id,
        )
        return dict(row)


async def cancel_reminder(user_id: int, reminder_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE reminders SET status = 'cancelled' WHERE id = $1 AND user_id = $2 RETURNING id",
            reminder_id, user_id,
        )
        return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Calendar events
# ---------------------------------------------------------------------------

async def list_calendar_events(user_id: int, upcoming_only: bool = True) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if upcoming_only:
            rows = await conn.fetch(
                "SELECT * FROM calendar_events WHERE user_id = $1 AND start_time >= NOW() "
                "AND status = 'scheduled' ORDER BY start_time",
                user_id,
            )
        else:
            rows = await conn.fetch(
                "SELECT * FROM calendar_events WHERE user_id = $1 ORDER BY start_time DESC",
                user_id,
            )
        return _rows(rows)


async def _insert_calendar_event(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    user_tz = payload.get("user_timezone")
    status_val = payload.get("status") or "scheduled"
    row = await conn.fetchrow(
        """
        INSERT INTO calendar_events (user_id, title, start_time, end_time, location, notes, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7::calendar_event_status) RETURNING *
        """,
        user_id, payload["title"],
        _parse_dt(payload["start_time"], user_tz), _parse_dt(payload["end_time"], user_tz),
        payload.get("location"), payload.get("notes"), status_val,
    )
    return dict(row)


async def _update_calendar_event(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    user_tz = payload.get("user_timezone")
    row = await conn.fetchrow(
        """
        UPDATE calendar_events SET
            title = COALESCE($3, title),
            start_time = COALESCE($4, start_time),
            end_time = COALESCE($5, end_time),
            location = COALESCE($6, location),
            notes = COALESCE($7, notes),
            status = COALESCE($8::calendar_event_status, status),
            updated_at = NOW()
        WHERE id = $1 AND user_id = $2
        RETURNING *
        """,
        payload["event_id"], user_id, payload.get("title"), _parse_dt(payload.get("start_time"), user_tz),
        _parse_dt(payload.get("end_time"), user_tz), payload.get("location"), payload.get("notes"), payload.get("status"),
    )
    return dict(row) if row else {}


async def _delete_calendar_event(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        "UPDATE calendar_events SET status = 'cancelled' WHERE id = $1 AND user_id = $2 RETURNING id",
        payload["event_id"], user_id,
    )
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Notes -- fully direct/ungated (creating AND deleting a note is low-stakes
# and immediately reversible, so neither goes through pending_actions).
# ---------------------------------------------------------------------------

async def create_note(user_id: int, content: str, tags: str | None = None) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "INSERT INTO notes (user_id, content, tags) VALUES ($1, $2, $3) RETURNING *",
            user_id, content, tags,
        )
        return dict(row)


async def list_notes(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM notes WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
        return _rows(rows)


async def delete_note(user_id: int, note_id: int) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM notes WHERE id = $1 AND user_id = $2 RETURNING id",
            note_id, user_id,
        )
        return dict(row) if row else {}


# ---------------------------------------------------------------------------
# People / contacts -- fully direct/ungated, including delete (previously
# missing entirely: contacts could be created/updated via upsert_person but
# never removed at all).
# ---------------------------------------------------------------------------

async def upsert_person(
    user_id: int,
    name: str,
    relationship_type: str | None = None,
    notes: str | None = None,
    phone_number: str | None = None,
    email: str | None = None,
) -> dict[str, Any]:
    """`phone_number`/`email` (migrations/017_contacts_phone_email.sql) let
    Messa actually reach a saved contact later -- e.g. resolving "email Sam
    about the invoice" to Sam's own saved address via find_person_by_name
    below, without the user having to repeat it every time. Guarded by
    _has_column so a deployment that hasn't run migration 017 yet still
    saves the columns it already understands (name/relationship_type/
    notes) instead of erroring on the whole call -- same degrade-gracefully
    pattern as everywhere else in this file that adds columns to an
    existing table."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        has_contact_info = await _has_column(conn, "people", "phone_number")
        if has_contact_info:
            row = await conn.fetchrow(
                """
                INSERT INTO people (user_id, name, relationship_type, notes, phone_number, email)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT ON CONSTRAINT uq_people_user_name
                DO UPDATE SET
                    relationship_type = COALESCE(EXCLUDED.relationship_type, people.relationship_type),
                    notes = COALESCE(EXCLUDED.notes, people.notes),
                    phone_number = COALESCE(EXCLUDED.phone_number, people.phone_number),
                    email = COALESCE(EXCLUDED.email, people.email),
                    updated_at = NOW()
                RETURNING *
                """,
                user_id, name, relationship_type, notes, phone_number, email,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO people (user_id, name, relationship_type, notes)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT ON CONSTRAINT uq_people_user_name
                DO UPDATE SET
                    relationship_type = COALESCE(EXCLUDED.relationship_type, people.relationship_type),
                    notes = COALESCE(EXCLUDED.notes, people.notes),
                    updated_at = NOW()
                RETURNING *
                """,
                user_id, name, relationship_type, notes,
            )
        return dict(row)


async def list_people(user_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM people WHERE user_id = $1 ORDER BY name", user_id)
        return _rows(rows)


async def find_person_by_name(user_id: int, name: str) -> list[dict[str, Any]]:
    """Case-insensitive, partial-match contact lookup -- e.g. "Sam" matches
    a saved "Samantha Lee" -- for resolving a name the user mentioned
    ("email Sam about the invoice") to a saved phone_number/email before
    delegating to whichever email/text subagent actually sends it. Returns
    EVERY match, not just the first: a caller with two "Sam"s saved can
    then ask the user which one instead of silently guessing wrong. An
    empty list means no contact by that name exists yet. Works fine
    pre-migration-017 too (no phone_number/email columns yet) -- those two
    keys are just absent/None on the returned rows rather than the whole
    call erroring, same pattern as upsert_person above."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM people WHERE user_id = $1 AND name ILIKE $2 ORDER BY name",
            user_id, f"%{name}%",
        )
        return _rows(rows)


async def delete_person(user_id: int, name: str) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "DELETE FROM people WHERE user_id = $1 AND name = $2 RETURNING id, name",
            user_id, name,
        )
        return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Cron jobs (create is gated via pending_actions; pause/cancel are direct)
# ---------------------------------------------------------------------------

async def list_cron_jobs(user_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cron_jobs WHERE user_id = $1 ORDER BY next_run_at", user_id
        )
        return _rows(rows)


async def get_due_cron_jobs(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM cron_jobs WHERE status = 'active' AND next_run_at <= $1", now
        )
        return _rows(rows)


async def get_due_cron_jobs_for_delivery(
    now: datetime | None = None, exclude_kinds: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Same as get_due_cron_jobs, but joined with users for the phone_number
    a production re-invocation needs -- used by server.py's real cron
    poller, which re-invokes Messa's full LLM turn for whatever this
    returns.

    `exclude_kinds` (added alongside messa/briefings.py): server.py's
    _production_cron_loop passes config.DEFAULT_BRIEFINGS' keys here so the
    two system-provisioned briefing jobs -- now rendered deterministically
    and delivered in parallel by the separate _production_briefing_loop/
    get_due_briefing_jobs_for_delivery below -- are never ALSO picked up
    and re-run through a full (sequential, per-job) LLM turn by this
    function's caller. None (the default, unchanged from before this
    param existed) excludes nothing, for any other caller. Pre-migration-011
    (no `kind` column), every job is still untagged (kind IS NULL) and this
    filter is simply never true, so nothing is excluded -- safe no-op."""
    now = now or datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if exclude_kinds and await _has_column(conn, "cron_jobs", "kind"):
            rows = await conn.fetch(
                """
                SELECT c.*, u.phone_number
                FROM cron_jobs c JOIN users u ON u.id = c.user_id
                WHERE c.status = 'active' AND c.next_run_at <= $1
                  AND (c.kind IS NULL OR NOT (c.kind = ANY($2::text[])))
                """,
                now, list(exclude_kinds),
            )
        else:
            rows = await conn.fetch(
                """
                SELECT c.*, u.phone_number
                FROM cron_jobs c JOIN users u ON u.id = c.user_id
                WHERE c.status = 'active' AND c.next_run_at <= $1
                """,
                now,
            )
        return _rows(rows)


async def get_due_briefing_jobs_for_delivery(now: datetime | None = None) -> list[dict[str, Any]]:
    """Same join as get_due_cron_jobs_for_delivery, scoped to just the two
    system-provisioned briefing kinds (config.DEFAULT_BRIEFINGS) and with
    the extra user profile fields messa/briefings.py's deterministic
    template renderer needs -- name (for the greeting) and latitude/
    longitude (migrations/019_user_coordinates.sql, for weather.py) --
    that an arbitrary user-created automation never required (it only ever
    needed phone_number, to re-invoke Messa with its saved prompt).

    Used by server.py's _production_briefing_loop, which renders and sends
    these itself (template-based, fanned out in parallel via asyncio.gather)
    instead of letting _production_cron_loop's per-job sequential LLM turn
    handle them -- see that function's own exclude_kinds param for the
    other half of that split.

    Returns [] (not an error) pre-migration-011 (no `kind` column at all,
    so a briefing job can't be identified as one) -- _production_cron_loop
    then still picks these rows up as ordinary untagged jobs, exactly the
    behavior this whole feature had before kind-tagging existed. Coordinates
    come back as None pre-migration-019 (guarded independently), same
    "degrade the optional part, not the whole query" pattern used
    throughout this module -- weather.py's caller already treats a missing
    latitude/longitude as "no weather line," so this needs no special
    handling here."""
    now = now or datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "cron_jobs", "kind"):
            return []
        kinds = list(config.DEFAULT_BRIEFINGS.keys())
        if await _has_column(conn, "users", "latitude"):
            rows = await conn.fetch(
                """
                SELECT c.*, u.phone_number, u.name AS user_name, u.latitude, u.longitude
                FROM cron_jobs c JOIN users u ON u.id = c.user_id
                WHERE c.status = 'active' AND c.next_run_at <= $1 AND c.kind = ANY($2::text[])
                """,
                now, kinds,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT c.*, u.phone_number, u.name AS user_name
                FROM cron_jobs c JOIN users u ON u.id = c.user_id
                WHERE c.status = 'active' AND c.next_run_at <= $1 AND c.kind = ANY($2::text[])
                """,
                now, kinds,
            )
        return _rows(rows)


async def reschedule_cron_job(cron_id: int, next_run_at: datetime) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE cron_jobs SET last_run_at = NOW(), next_run_at = $2 WHERE id = $1",
            cron_id, next_run_at,
        )


async def set_cron_job_status(user_id: int, cron_id: int, status: str) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE cron_jobs SET status = $3 WHERE id = $1 AND user_id = $2 RETURNING *",
            cron_id, user_id, status,
        )
        return dict(row) if row else {}


async def _insert_cron_job(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    # payload["next_run_at"] is already a tz-aware datetime by the time it
    # gets here (tools/routines_tools.py computes it via compute_next_run,
    # which is timezone-aware end to end) -- no string parsing needed.
    row = await conn.fetchrow(
        """
        INSERT INTO cron_jobs (user_id, prompt_or_task, cron_expression, user_timezone, next_run_at, status)
        VALUES ($1, $2, $3, $4, $5, 'active'::cron_job_status) RETURNING *
        """,
        user_id, payload["prompt_or_task"], payload["cron_expression"],
        payload.get("user_timezone", config.DEFAULT_TIMEZONE), payload["next_run_at"],
    )
    return dict(row)


def _compute_next_run_local(cron_expression: str, tz_name: str) -> datetime:
    # Deliberately duplicated (not imported) from tools/routines_tools.py's
    # compute_next_run: that module does `from .. import db`, so db
    # importing back from it would be circular. Same three lines either way.
    tz = ZoneInfo(tz_name)
    return croniter(cron_expression, datetime.now(tz)).get_next(datetime)


async def _retire_legacy_briefing_if_any(conn: asyncpg.Connection, user_id: int, kind: str) -> None:
    """Before creating a NEW tagged briefing row, cancel an old, untagged
    one that's already serving the same purpose -- see
    config.LEGACY_BRIEFING_MATCH_KEYWORDS' own comment for the "replace
    entirely" decision this implements. `kind IS NULL` scopes this to jobs
    that predate the `kind` column entirely (an ordinary user-created job
    via propose_create_recurring_cron); a job this function itself tagged
    on an earlier call is never a candidate for re-matching here. Matches
    on exactly ONE candidate only -- zero or multiple candidates means
    "don't touch anything," since guessing wrong here means cancelling a
    real user's real automation."""
    keywords = config.LEGACY_BRIEFING_MATCH_KEYWORDS.get(kind, ())
    if not keywords:
        return
    candidates = await conn.fetch(
        "SELECT id, prompt_or_task FROM cron_jobs WHERE user_id = $1 AND kind IS NULL AND status != 'cancelled'",
        user_id,
    )
    matches = [
        c for c in candidates
        if any(kw in (c["prompt_or_task"] or "").lower() for kw in keywords)
    ]
    if len(matches) != 1:
        return
    await conn.execute("UPDATE cron_jobs SET status = 'cancelled' WHERE id = $1", matches[0]["id"])


async def ensure_default_briefings(user_row: dict[str, Any]) -> None:
    """Auto-provisions the morning + evening briefing cron jobs (see
    config.DEFAULT_BRIEFINGS) for a user who doesn't already have one of
    each -- every user gets both by default, new or existing, rather than
    needing to think to ask Messa to set one up. Called from
    cli.load_user_context on every turn (same "self-heals on load" pattern
    as ensure_timezone_resolved, right after it) and from get_or_create_user
    for a brand-new row, so this covers both "new user" and "existing user"
    without a separate one-off backfill script.

    Tags each row it creates with `kind` (migrations/011_cron_job_kind.sql)
    so a later call can tell "already has one" apart from "never had one"
    without re-creating a briefing the user deliberately cancelled or
    paused -- existence of a row with this kind, regardless of its current
    status, is what's checked. This is also why it writes directly rather
    than going through propose_action/create_recurring_cron's confirmation
    gate: that gate exists for a MODEL deciding to create new standing
    automation on its own initiative, not for a default the product itself
    turns on for everyone, the same reasoning get_or_create_live_share_token
    already applies to auto-issuing a live-view link.

    Also self-heals timing: if a briefing row's stored user_timezone no
    longer matches the user's own (now-confirmed) timezone -- e.g. it was
    provisioned before onboarding resolved a real city, using
    config.DEFAULT_TIMEZONE as a placeholder -- this corrects that row's
    user_timezone and recomputes next_run_at, so "7am" actually means 7am
    where the user lives rather than wherever the placeholder pointed.
    Additive/no-op-safe: does nothing at all if migration 011 hasn't been
    applied yet.

    Before creating a brand-new row for a kind this user has never had
    tagged, first checks for (and retires) an old, untagged job already
    serving that same purpose from before this feature existed -- see
    _retire_legacy_briefing_if_any and config.LEGACY_BRIEFING_MATCH_KEYWORDS.
    Explicit product decision: replace those entirely rather than leave a
    duplicate running alongside the new one.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "cron_jobs", "kind"):
            return
        user_id = user_row["id"]
        tz_name = user_row.get("timezone") or config.DEFAULT_TIMEZONE
        timezone_confirmed = bool(user_row.get("timezone_confirmed"))

        for kind, spec in config.DEFAULT_BRIEFINGS.items():
            existing = await conn.fetchrow(
                "SELECT id, user_timezone FROM cron_jobs WHERE user_id = $1 AND kind = $2",
                user_id, kind,
            )
            if existing is None:
                await _retire_legacy_briefing_if_any(conn, user_id, kind)
                next_run = _compute_next_run_local(spec["cron_expression"], tz_name)
                await conn.execute(
                    """
                    INSERT INTO cron_jobs
                        (user_id, prompt_or_task, cron_expression, user_timezone, next_run_at, kind, status)
                    VALUES ($1, $2, $3, $4, $5, $6, 'active'::cron_job_status)
                    """,
                    user_id, spec["prompt_or_task"], spec["cron_expression"], tz_name, next_run, kind,
                )
            elif timezone_confirmed and existing["user_timezone"] != tz_name:
                next_run = _compute_next_run_local(spec["cron_expression"], tz_name)
                await conn.execute(
                    "UPDATE cron_jobs SET user_timezone = $2, next_run_at = $3 WHERE id = $1",
                    existing["id"], tz_name, next_run,
                )


# ---------------------------------------------------------------------------
# Pending actions -- the confirm-before-write gate.
#
# Per explicit product decision, only SCHEDULING requires confirmation now:
# create/update/delete calendar event, and creating a new recurring
# automation (routines_agent's cron jobs -- also a scheduling action).
# Tasks, reminders, notes, and contacts all write directly (see their
# sections above) since they're low-stakes and trivially reversible with a
# follow-up message -- an earlier version of this gate also covered those,
# which added confirmation friction the product decision explicitly removed.
# ---------------------------------------------------------------------------

_APPLIERS = {
    "create_calendar_event": _insert_calendar_event,
    "update_calendar_event": _update_calendar_event,
    "delete_calendar_event": _delete_calendar_event,
    "create_recurring_cron": _insert_cron_job,
}

GATED_ACTION_TYPES = set(_APPLIERS)


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    raise TypeError(f"Not JSON serializable: {o!r}")


async def propose_action(
    user_id: int, action_type: str, payload: dict[str, Any], ttl_minutes: int = 30
) -> dict[str, Any]:
    if action_type not in GATED_ACTION_TYPES:
        raise ValueError(f"Unknown gated action_type: {action_type}")
    pool = await get_pool()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO pending_actions (user_id, action_type, payload, expires_at)
            VALUES ($1, $2, $3, $4) RETURNING *
            """,
            user_id, action_type, json.dumps(payload, default=_json_default), expires_at,
        )
        return dict(row)


async def get_pending_action(user_id: int, pending_action_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM pending_actions WHERE id = $1 AND user_id = $2",
            pending_action_id, user_id,
        )
        return dict(row) if row else None


async def list_pending_actions(user_id: int, state: str = "pending") -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM pending_actions WHERE user_id = $1 AND state = $2 ORDER BY created_at",
            user_id, state,
        )
        return _rows(rows)


async def confirm_pending_action(user_id: int, pending_action_id: int) -> dict[str, Any]:
    """Apply a pending action's payload to the real table, log it, mark confirmed."""
    action = await get_pending_action(user_id, pending_action_id)
    if action is None:
        return {"ok": False, "error": "No such pending action."}
    if action["state"] != "pending":
        return {"ok": False, "error": f"Action is already '{action['state']}', not pending."}
    if action["expires_at"] < datetime.now(timezone.utc):
        await set_pending_action_state(pending_action_id, "expired")
        return {"ok": False, "error": "This action expired. Please ask again."}

    applier = _APPLIERS[action["action_type"]]
    payload = json.loads(action["payload"])

    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            result = await applier(conn, user_id, payload)
            await conn.execute(
                "UPDATE pending_actions SET state = 'confirmed' WHERE id = $1", pending_action_id
            )
            await conn.execute(
                """
                INSERT INTO audit_logs (user_id, action_type, payload, source_pending_action_id)
                VALUES ($1, $2, $3, $4)
                """,
                user_id, action["action_type"], action["payload"], pending_action_id,
            )
    return {"ok": True, "action_type": action["action_type"], "result": result}


async def set_pending_action_state(pending_action_id: int, state: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE pending_actions SET state = $2 WHERE id = $1", pending_action_id, state)


async def reject_pending_action(user_id: int, pending_action_id: int) -> dict[str, Any]:
    action = await get_pending_action(user_id, pending_action_id)
    if action is None:
        return {"ok": False, "error": "No such pending action."}
    await set_pending_action_state(pending_action_id, "cancelled")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Projects (additive concept, not in the original schema -- see
# migrations/002_projects_and_channel.sql). Falls back to no-ops if the
# migration hasn't been applied yet, so the rest of the app still works.
# ---------------------------------------------------------------------------

async def _has_table(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=$1)",
        table,
    ))


async def get_or_create_project(user_id: int, title: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "projects"):
            return None
        row = await conn.fetchrow(
            "SELECT * FROM projects WHERE user_id = $1 AND title = $2 AND status = 'active'",
            user_id, title,
        )
        if row:
            return dict(row)
        row = await conn.fetchrow(
            "INSERT INTO projects (user_id, title) VALUES ($1, $2) RETURNING *",
            user_id, title,
        )
        return dict(row)


async def list_projects(user_id: int, status: str = "active") -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "projects"):
            return []
        rows = await conn.fetch(
            "SELECT * FROM projects WHERE user_id = $1 AND status = $2 ORDER BY updated_at DESC",
            user_id, status,
        )
        return _rows(rows)


# ---------------------------------------------------------------------------
# Deepsearch sessions -- resumable research/browsing threads.
#
# Deliberately framework-agnostic here, same as the rest of this file:
# `messages_json` is just a string this layer stores and returns as-is.
# tools/deepsearch_tools.py is what knows it's a LangChain message list
# (via messages_to_dict/messages_from_dict) -- db.py doesn't import
# LangChain. Additive: no-ops (returns None/[]) if migration 004 hasn't
# been applied yet, same pattern as the projects table.
# ---------------------------------------------------------------------------

async def create_deepsearch_session(user_id: int, title: str, messages_json: str = "[]") -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_sessions"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO deepsearch_sessions (user_id, title, messages)
            VALUES ($1, $2, $3) RETURNING *
            """,
            user_id, title[:255], messages_json,
        )
        return dict(row)


async def has_prior_deepsearch_session(user_id: int) -> bool:
    """Cheap existence check -- true if this user has EVER had a deepsearch
    session (any status), false for a brand-new user's very first one.

    Used by cli.py's run_message to gate a one-time "I can do more than
    just browsing" tip onto the acknowledgment right before the FIRST-ever
    deepsearch delegation, so it doesn't repeat on every single browsing
    task after that -- deliberately a lightweight EXISTS query rather than
    reusing list_deepsearch_sessions (which pulls up to 20 full rows), since
    this runs inline on the hot path of every deepsearch delegation, not
    just when the user explicitly asks to see their session list. Called
    BEFORE the current delegation's own session row is created (see
    build_deepsearch_subagent's _run), so it correctly reports False on that
    very first delegation, not True."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_sessions"):
            return False
        return bool(
            await conn.fetchval("SELECT EXISTS (SELECT 1 FROM deepsearch_sessions WHERE user_id = $1)", user_id)
        )


async def get_deepsearch_session(user_id: int, session_id: int) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_sessions"):
            return None
        row = await conn.fetchrow(
            "SELECT * FROM deepsearch_sessions WHERE id = $1 AND user_id = $2",
            session_id, user_id,
        )
        return dict(row) if row else None


async def update_deepsearch_session(
    session_id: int,
    messages_json: str,
    status: str,
    summary: str | None,
    steps_used: int,
    live_view_url: str | None = None,
) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_sessions"):
            return None
        has_live_view = await _has_column(conn, "deepsearch_sessions", "live_view_url")
        if has_live_view:
            row = await conn.fetchrow(
                """
                UPDATE deepsearch_sessions SET
                    messages = $2, status = $3::deepsearch_status, summary = $4,
                    steps_used = $5, live_view_url = COALESCE($6, live_view_url), updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                session_id, messages_json, status, summary, steps_used, live_view_url,
            )
        else:
            # Migration 005 not applied yet -- live_view_url silently dropped.
            row = await conn.fetchrow(
                """
                UPDATE deepsearch_sessions SET
                    messages = $2, status = $3::deepsearch_status, summary = $4,
                    steps_used = $5, updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                session_id, messages_json, status, summary, steps_used,
            )
        return dict(row) if row else None


async def list_deepsearch_sessions(user_id: int, status: str | None = None) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_sessions"):
            return []
        if status:
            rows = await conn.fetch(
                "SELECT id, title, status, summary, steps_used, updated_at FROM deepsearch_sessions "
                "WHERE user_id = $1 AND status = $2::deepsearch_status ORDER BY updated_at DESC LIMIT 20",
                user_id, status,
            )
        else:
            rows = await conn.fetch(
                "SELECT id, title, status, summary, steps_used, updated_at FROM deepsearch_sessions "
                "WHERE user_id = $1 ORDER BY updated_at DESC LIMIT 20",
                user_id,
            )
        return _rows(rows)


# ---------------------------------------------------------------------------
# Human-in-the-loop pause (deepsearch hit a login wall/CAPTCHA/2FA and is
# waiting on you) -- see migrations/008_deepsearch_human_help.sql and
# tools/deepsearch_tools.py's request_human_help tool for the full flow.
# Additive/no-op-safe, same _has_table pattern as deepsearch_sessions above:
# these all silently no-op until migration 008 has been applied.
#
# create_human_help_request is called from inside the (in-flight,
# in-process) deepsearch agent call itself; get_waiting_human_help_requests/
# mark_human_help_notified are polled from server.py's separate
# _production_deepsearch_pause_loop -- deliberately two different call
# sites, so the SMS notification doesn't depend on the same async call that
# created the row still being alive/healthy.
# ---------------------------------------------------------------------------

async def create_human_help_request(
    user_id: int, deepsearch_session_id: int | None, tab_marker: str, reason: str,
) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_human_help_requests"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO deepsearch_human_help_requests (user_id, deepsearch_session_id, tab_marker, reason)
            VALUES ($1, $2, $3, $4) RETURNING *
            """,
            user_id, deepsearch_session_id, tab_marker, reason,
        )
        return dict(row)


async def get_waiting_human_help_requests() -> list[dict[str, Any]]:
    """All still-'waiting' rows across every user, joined with phone_number
    -- this is what _production_deepsearch_pause_loop polls every few
    seconds. Deliberately not scoped to one user (unlike most of this
    file): the poll loop, like the reminder/cron loops it mirrors, is a
    single global background task covering every user at once."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_human_help_requests"):
            return []
        rows = await conn.fetch(
            """
            SELECT h.*, u.phone_number
            FROM deepsearch_human_help_requests h JOIN users u ON u.id = h.user_id
            WHERE h.status = 'waiting'
            """,
        )
        return _rows(rows)


async def mark_human_help_notified(request_id: int) -> None:
    """Sets notified_at the first (and only) time the poll loop sends the
    SMS for this row -- get_waiting_human_help_requests keeps returning the
    row on every poll until it's resolved/timed out, so the caller checks
    notified_at itself before deciding to send again."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_human_help_requests"):
            return
        await conn.execute(
            "UPDATE deepsearch_human_help_requests SET notified_at = NOW() WHERE id = $1 AND notified_at IS NULL",
            request_id,
        )


async def resolve_human_help_request(request_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_human_help_requests"):
            return
        await conn.execute(
            "UPDATE deepsearch_human_help_requests SET status = 'resolved', resolved_at = NOW() WHERE id = $1",
            request_id,
        )


async def timeout_human_help_request(request_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_human_help_requests"):
            return
        await conn.execute(
            "UPDATE deepsearch_human_help_requests SET status = 'timed_out', resolved_at = NOW() WHERE id = $1",
            request_id,
        )


# ---------------------------------------------------------------------------
# Gmail connection requests (migrations/012_email_connection.sql) -- durable
# state for "connect my email", same decoupled shape as the human-help block
# above: request_email_connection (tools/email_tools.py) creates the row the
# moment it generates a Composio connect link; server.py's separate
# _production_email_connection_poll_loop polls Composio for that row's real
# status and sends exactly one confirmation text once it's active.
# Additive/no-op-safe: all no-op until migration 012 has been applied.
# ---------------------------------------------------------------------------

async def create_email_connection_request(user_id: int, connected_account_id: str | None) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "email_connection_requests"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO email_connection_requests (user_id, connected_account_id)
            VALUES ($1, $2) RETURNING *
            """,
            user_id, connected_account_id,
        )
        return dict(row)


async def get_pending_email_connection_requests() -> list[dict[str, Any]]:
    """All still-'pending' rows across every user, joined with phone_number
    -- what _production_email_connection_poll_loop polls. Excludes rows
    older than config.EMAIL_CONNECTION_REQUEST_EXPIRES_HOURS; the loop
    expires those separately (see expire_stale_email_connection_requests)
    rather than this function silently hiding them forever."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "email_connection_requests"):
            return []
        rows = await conn.fetch(
            """
            SELECT e.*, u.phone_number
            FROM email_connection_requests e JOIN users u ON u.id = e.user_id
            WHERE e.status = 'pending'
            AND e.requested_at > NOW() - ($1 || ' hours')::interval
            """,
            str(config.EMAIL_CONNECTION_REQUEST_EXPIRES_HOURS),
        )
        return _rows(rows)


async def expire_stale_email_connection_requests() -> list[dict[str, Any]]:
    """Silently expires (no notification -- the user can just ask again)
    any 'pending' row older than the configured window, so the poll loop's
    working set and the table itself don't grow forever with abandoned
    requests. Returns the rows it expired, in case a caller wants to log
    how many."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "email_connection_requests"):
            return []
        rows = await conn.fetch(
            """
            UPDATE email_connection_requests
            SET status = 'expired', resolved_at = NOW()
            WHERE status = 'pending' AND requested_at <= NOW() - ($1 || ' hours')::interval
            RETURNING *
            """,
            str(config.EMAIL_CONNECTION_REQUEST_EXPIRES_HOURS),
        )
        return _rows(rows)


async def mark_email_connection_notified(request_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "email_connection_requests"):
            return
        await conn.execute(
            "UPDATE email_connection_requests SET notified_at = NOW() WHERE id = $1 AND notified_at IS NULL",
            request_id,
        )


async def mark_email_connected(request_id: int, user_id: int) -> None:
    """Called once Composio confirms the connection is ACTIVE: resolves the
    request row AND flips the cheap users.email_connected cache in the same
    transaction, so the two can't drift apart."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if await _has_table(conn, "email_connection_requests"):
                await conn.execute(
                    "UPDATE email_connection_requests SET status = 'active', resolved_at = NOW() WHERE id = $1",
                    request_id,
                )
            if await _has_column(conn, "users", "email_connected"):
                await conn.execute(
                    "UPDATE users SET email_connected = TRUE WHERE id = $1", user_id,
                )


async def expire_email_connection_request(request_id: int) -> None:
    """Called when Composio reports a terminal failure (FAILED/EXPIRED/
    REVOKED) for a request still marked 'pending' -- distinct from the
    silent age-based expiry above, this one's worth telling the user about
    (see _production_email_connection_poll_loop), so it's a separate call
    even though the DB write is identical to one row of the bulk version."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "email_connection_requests"):
            return
        await conn.execute(
            "UPDATE email_connection_requests SET status = 'expired', resolved_at = NOW() WHERE id = $1",
            request_id,
        )


async def get_active_email_connection(user_id: int) -> dict[str, Any] | None:
    """The user's currently-ACTIVE email_connection_requests row (the one
    carrying the real connected_account_id Composio needs to actually
    disconnect it), or None if they have none right now -- used by
    tools/email_tools.py's new disconnect_email tool and
    request_email_connection's new switch_account param (migrations/
    022_disconnect_integrations.sql). Most-recently-resolved first in the
    (should never happen, but a defensive tie-break) case of more than
    one somehow being ACTIVE at once."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "email_connection_requests"):
            return None
        row = await conn.fetchrow(
            """
            SELECT * FROM email_connection_requests
            WHERE user_id = $1 AND status = 'active'
            ORDER BY resolved_at DESC NULLS LAST, requested_at DESC
            LIMIT 1
            """,
            user_id,
        )
        return dict(row) if row else None


async def mark_email_disconnected(request_id: int, user_id: int) -> None:
    """The disconnect-side mirror of mark_email_connected -- resolves the
    request row (status='disconnected', migrations/
    022_disconnect_integrations.sql) AND flips users.email_connected back
    to FALSE in the same transaction, so the two can't drift apart, same
    reasoning as mark_email_connected's own docstring. Called only after
    Composio's own connected_accounts.delete call has already succeeded
    (see tools/email_tools.py's disconnect_email) -- this is just the
    local bookkeeping catching up to what Composio already did."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            if await _has_table(conn, "email_connection_requests"):
                await conn.execute(
                    "UPDATE email_connection_requests SET status = 'disconnected', resolved_at = NOW() WHERE id = $1",
                    request_id,
                )
            if await _has_column(conn, "users", "email_connected"):
                await conn.execute(
                    "UPDATE users SET email_connected = FALSE WHERE id = $1", user_id,
                )


# ---------------------------------------------------------------------------
# Dynamic Integration Engine (migrations/020_dynamic_integrations.sql) --
# see that migration's own header comment for the shape/reasoning. Three
# groups below, same additive/no-op-safe pattern as everything else in this
# file: every function here does nothing (returns None/[]/False) rather
# than raising if migration 020 hasn't been applied yet.
# ---------------------------------------------------------------------------

async def get_app_preference(user_id: int, app_category: str) -> str | None:
    """The user's preferred app for a category ('email', 'calendar', or
    'tasks' as of the primary-app preference system -- see
    agents/registry.py's set_app_preference/list_my_connected_apps tools
    and its "Known about this user" prompt block for how this actually
    drives routing), or None if they've never set one (meaning: still on
    Messa's own native tool for that category) -- callers treat None as
    'messa', same as this always meant for email before this function had
    any callers.

    Category 'email' specifically has a legacy fallback: migrations/
    015_default_email_provider.sql shipped BEFORE this generic table did,
    as its own dedicated users.default_email_provider enum column ('messa'/
    'gmail'), and every existing user already has a real choice recorded
    there. Rather than a one-time backfill migration (extra deploy step,
    extra thing that can go wrong), this reads through to that column
    whenever 'email' has no row here yet -- zero-downtime, and every
    existing user's current choice is preserved automatically. Once
    set_app_preference (below) is ever called for 'email', a real row
    exists here and this fallback no longer matters for that user; the
    column itself is still kept in sync too (see set_app_preference), so
    the other, older code paths that still read
    UserContext.default_email_provider directly (a cheap per-turn read
    with no query at all -- see cli.py's load path) keep working
    unchanged."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await _has_table(conn, "user_app_preferences"):
            value = await conn.fetchval(
                "SELECT preferred_app FROM user_app_preferences WHERE user_id = $1 AND app_category = $2",
                user_id, app_category,
            )
            if value is not None:
                return value
        if app_category == "email" and await _has_column(conn, "users", "default_email_provider"):
            value = await conn.fetchval(
                "SELECT default_email_provider FROM users WHERE id = $1", user_id,
            )
            # The column's own DEFAULT is 'messa' -- every user has SOME
            # value here once the column exists, so a non-null result is
            # always meaningful, never itself a "not set" signal.
            return value
        return None


# Categories whose legacy value must round-trip through
# users.default_email_provider's own ENUM ('messa'/'gmail' only, enforced
# at the DB level) -- see get_app_preference's docstring. A category with
# no such column (calendar, tasks), or an 'email' value the enum doesn't
# recognize (e.g. a future 'outlook' connection), simply isn't written
# through -- user_app_preferences is ALREADY the source of truth for those
# the moment a real row exists, the column is only ever a legacy mirror for
# the two values it understands.
_EMAIL_PROVIDER_ENUM_VALUES = {"messa", "gmail"}


async def set_app_preference(user_id: int, app_category: str, preferred_app: str) -> dict[str, Any] | None:
    """Upsert -- always at the user's own explicit request ('use Todoist
    for my tasks by default', 'make Google Calendar my primary calendar',
    'use my gmail as my main email') via agents/registry.py's
    set_app_preference tool, or the connect-time auto-promote/ask-on-
    conflict flow in server.py's connection poll loops (see
    _production_email_connection_poll_loop/_production_app_connection_poll_loop) --
    never anything else, same one-writer-only-at-explicit-request shape as
    the original set_default_email_provider always had.

    For app_category == 'email' specifically, this ALSO writes through to
    the legacy users.default_email_provider column (via
    set_default_email_provider) whenever preferred_app is a value that
    column's ENUM actually accepts ('messa'/'gmail') -- keeps every other
    still-existing direct read of UserContext.default_email_provider (a
    zero-query per-turn field, see cli.py) correctly in sync, rather than
    only correct through this table. A value the enum can't represent
    (e.g. 'outlook') just skips that write-through -- this table alone is
    authoritative for it, no error, nothing left inconsistent since
    default_email_provider was never going to be able to say 'outlook'
    anyway."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "user_app_preferences"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO user_app_preferences (user_id, app_category, preferred_app)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id, app_category) DO UPDATE SET preferred_app = $3, updated_at = NOW()
            RETURNING *
            """,
            user_id, app_category, preferred_app,
        )
        result = dict(row)

    if app_category == "email" and preferred_app in _EMAIL_PROVIDER_ENUM_VALUES:
        try:
            await set_default_email_provider(user_id, preferred_app)
        except Exception as e:  # noqa: BLE001 - the new table is now authoritative either way; the legacy column is a best-effort mirror
            console.system(f"set_app_preference: legacy default_email_provider write-through failed (non-fatal): {e}")

    return result


async def log_unsupported_integration_request(user_id: int, requested_app_name: str, raw_user_prompt: str) -> None:
    """Fire-and-forget product-roadmap signal: called whenever
    search_integration_tools finds no Composio toolkit at all matching the
    query, independent of whether the deepsearch/Browserbase fallback then
    manages to handle the request anyway -- the point is "users want X",
    not "we couldn't help them right now". Best-effort: swallows its own
    failure (never lets a logging problem break the actual request the
    user is waiting on) beyond the standard no-op-if-migration-missing
    guard every other function here already gets."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "unsupported_integration_requests"):
            return
        await conn.execute(
            """
            INSERT INTO unsupported_integration_requests (user_id, requested_app_name, raw_user_prompt)
            VALUES ($1, $2, $3)
            """,
            user_id, requested_app_name[:128], raw_user_prompt,
        )


# --- app_connection_requests: same decoupled propose-now/notify-later shape
# as email_connection_requests above, generalized to any Composio toolkit.
# See migrations/020_dynamic_integrations.sql's header and server.py's
# _production_app_connection_poll_loop for the full flow. ---

async def create_app_connection_request(
    user_id: int, toolkit_slug: str, connected_account_id: str | None
) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO app_connection_requests (user_id, toolkit_slug, connected_account_id)
            VALUES ($1, $2, $3) RETURNING *
            """,
            user_id, toolkit_slug, connected_account_id,
        )
        return dict(row)


async def get_pending_app_connection_requests() -> list[dict[str, Any]]:
    """All still-'pending' rows across every user, joined with phone_number
    -- what _production_app_connection_poll_loop polls. Excludes rows older
    than config.APP_CONNECTION_REQUEST_EXPIRES_HOURS; the loop expires
    those separately (see expire_stale_app_connection_requests)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return []
        rows = await conn.fetch(
            """
            SELECT a.*, u.phone_number
            FROM app_connection_requests a JOIN users u ON u.id = a.user_id
            WHERE a.status = 'pending'
            AND a.requested_at > NOW() - ($1 || ' hours')::interval
            """,
            str(config.APP_CONNECTION_REQUEST_EXPIRES_HOURS),
        )
        return _rows(rows)


async def expire_stale_app_connection_requests() -> list[dict[str, Any]]:
    """Silently expires (no notification -- the user can just ask again)
    any 'pending' row older than the configured window."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return []
        rows = await conn.fetch(
            """
            UPDATE app_connection_requests
            SET status = 'expired', resolved_at = NOW()
            WHERE status = 'pending' AND requested_at <= NOW() - ($1 || ' hours')::interval
            RETURNING *
            """,
            str(config.APP_CONNECTION_REQUEST_EXPIRES_HOURS),
        )
        return _rows(rows)


async def mark_app_connected(request_id: int) -> None:
    """Called once Composio confirms the connection is ACTIVE. Unlike
    mark_email_connected, there's no per-app users.<x>_connected cache to
    flip here -- see migrations/020's header for why (1,400+ toolkits,
    unbounded); a connected app's live status is checked directly against
    Composio (search_integration_tools) when it's actually needed instead."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return
        await conn.execute(
            "UPDATE app_connection_requests SET status = 'active', resolved_at = NOW() WHERE id = $1",
            request_id,
        )


async def expire_app_connection_request(request_id: int) -> None:
    """Called when Composio reports a terminal failure (FAILED/EXPIRED/
    REVOKED) for a request still marked 'pending' -- distinct from the
    silent age-based expiry above, this one's worth telling the user about
    (see _production_app_connection_poll_loop)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return
        await conn.execute(
            "UPDATE app_connection_requests SET status = 'expired', resolved_at = NOW() WHERE id = $1",
            request_id,
        )


async def get_active_connected_toolkits(user_id: int) -> list[str]:
    """Toolkit slugs this user has an 'active' app_connection_requests row
    for (e.g. ['reddit', 'todoist']) -- a cheap, LOCAL-ONLY read used to
    give integrations_agent's own subagent description a per-turn hint of
    what's likely already connected (see docs/dynamic_connected_apps_spec.md
    and registry.py's _integrations_agent_description), without adding a
    live Composio API round trip to every single turn the way querying
    connected_accounts.list directly would.

    Deliberately advisory, not authoritative -- two real gaps, both
    accepted on purpose: (1) this only reflects apps connected THROUGH
    Messa's own connect_integration_app flow -- an app connected some
    other way (directly in Composio's dashboard, say) won't show up here;
    (2) nothing currently flips a row back out of 'active' if the user
    later revokes the connection outside Messa, so this can drift stale
    over time in the 'shows connected when it no longer is' direction.
    Neither gap matters much in practice because nothing safety-relevant
    reads this: it's a text hint informing what Messa says about what's
    connected, and the ACTUAL execution path (search_integration_tools)
    always re-checks Composio's connected_accounts live before treating
    anything as connected -- see integration_tools.py's own module
    docstring for why that live check is deliberately not cached at all,
    let alone here."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return []
        rows = await conn.fetch(
            """
            SELECT DISTINCT toolkit_slug FROM app_connection_requests
            WHERE user_id = $1 AND status = 'active'
            ORDER BY toolkit_slug
            """,
            user_id,
        )
        return [r["toolkit_slug"] for r in rows]


async def get_active_app_connection(user_id: int, toolkit_slug: str) -> dict[str, Any] | None:
    """The user's currently-ACTIVE app_connection_requests row for this ONE
    toolkit (carrying the real connected_account_id Composio needs to
    actually disconnect it) -- the generic-path mirror of
    get_active_email_connection above. Filtered by toolkit_slug (unlike
    that Gmail-only function) because this table covers every connected
    app for a user, not just one. Used by tools/integration_tools.py's new
    disconnect_integration_app tool and connect_integration_app's new
    switch_account param (migrations/022_disconnect_integrations.sql).
    Most-recently-resolved first as a defensive tie-break in the (should
    never happen) case of more than one somehow being ACTIVE at once for
    the same toolkit."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return None
        row = await conn.fetchrow(
            """
            SELECT * FROM app_connection_requests
            WHERE user_id = $1 AND toolkit_slug = $2 AND status = 'active'
            ORDER BY resolved_at DESC NULLS LAST, requested_at DESC
            LIMIT 1
            """,
            user_id, toolkit_slug,
        )
        return dict(row) if row else None


async def disconnect_app_connection(request_id: int) -> None:
    """The disconnect-side mirror of mark_app_connected -- resolves the
    request row to status='disconnected' (migrations/
    022_disconnect_integrations.sql). Unlike mark_email_disconnected,
    there's no per-app users.<x>_connected cache to flip here, same reason
    mark_app_connected's own docstring gives for the connect side (1,400+
    toolkits, unbounded). Called only after Composio's own
    connected_accounts.delete call has already succeeded (see
    tools/integration_tools.py's disconnect_integration_app) -- this is
    just the local bookkeeping catching up to what Composio already did."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "app_connection_requests"):
            return
        await conn.execute(
            "UPDATE app_connection_requests SET status = 'disconnected', resolved_at = NOW() WHERE id = $1",
            request_id,
        )


# ---------------------------------------------------------------------------
# Site credentials (migrations/021_site_credentials.sql) -- accounts
# deepsearch creates on the user's behalf on sites Composio doesn't
# support. Every function here stores/returns encrypted_password EXACTLY
# as given -- encryption/decryption itself lives in credentials.py, never
# here, so this file never sees a plaintext password (see that migration's
# own header for the full reasoning).
# ---------------------------------------------------------------------------

async def save_site_credential(
    user_id: int, site_name: str, username: str, encrypted_password: str, site_url: str | None = None
) -> dict[str, Any] | None:
    """INSERT ... ON CONFLICT DO NOTHING -- deliberately never UPDATEs an
    existing row. A second call for a site that already has a stored
    credential returns None (not the existing row, not an error) so the
    caller (tools/deepsearch_tools.py's generate_account_credential) can
    tell 'already had one, didn't touch it' apart from 'just created it' --
    overwriting here would desync the stored password from whatever the
    real site still has, effectively locking Messa out of an account she
    already created."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "site_credentials"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO site_credentials (user_id, site_name, site_url, username, encrypted_password)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id, site_name) DO NOTHING
            RETURNING *
            """,
            user_id, site_name, site_url, username, encrypted_password,
        )
        return dict(row) if row else None


async def get_site_credential(user_id: int, site_name: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "site_credentials"):
            return None
        row = await conn.fetchrow(
            "SELECT * FROM site_credentials WHERE user_id = $1 AND site_name = $2",
            user_id, site_name,
        )
        return dict(row) if row else None


async def list_site_credentials(user_id: int) -> list[dict[str, Any]]:
    """Site names + usernames only in spirit (callers decide what to show
    the user -- this returns full rows, encrypted_password included, since
    db.py itself has no opinion on decryption); used for "what accounts
    have you set up for me" -- never for guessing which credential to use
    without the user naming (or the model already knowing) the site."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "site_credentials"):
            return []
        rows = await conn.fetch(
            "SELECT * FROM site_credentials WHERE user_id = $1 ORDER BY created_at DESC", user_id,
        )
        return _rows(rows)


async def delete_site_credential(user_id: int, site_name: str) -> bool:
    """Explicit user control ("forget my password for X") -- does NOT
    delete or otherwise affect the account on the real site, only Messa's
    own stored copy. Returns whether a row was actually deleted."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "site_credentials"):
            return False
        result = await conn.execute(
            "DELETE FROM site_credentials WHERE user_id = $1 AND site_name = $2", user_id, site_name,
        )
        return result.endswith(" 1")


async def mark_site_credential_used(user_id: int, site_name: str) -> None:
    """Best-effort bookkeeping (last_used_at) -- called after
    get_account_credential successfully hands a stored credential back for
    a login. Never raises; a failure here shouldn't block the login
    itself."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "site_credentials"):
            return
        await conn.execute(
            "UPDATE site_credentials SET last_used_at = NOW() WHERE user_id = $1 AND site_name = $2",
            user_id, site_name,
        )


# ---------------------------------------------------------------------------
# Default email provider (migrations/015_default_email_provider.sql) -- which
# inbox Messa treats as the default for a generic "send/check my email"
# request that doesn't name one: 'messa' (their own Messa-owned address,
# see the personal-inbox section below) or 'gmail' (the Gmail connection
# block above). Every row -- existing users at migration time, and every
# user created afterward -- starts at 'messa' via the column's own DEFAULT,
# so there's no separate backfill step here. The ONLY writer is
# agents/registry.py's set_default_email_provider tool, called at the
# user's own explicit request ("use my gmail by default") -- connecting
# Gmail (mark_email_connected above) never touches this column itself, by
# deliberate design: connecting an inbox and making it the default are two
# separate decisions.
# ---------------------------------------------------------------------------

async def set_default_email_provider(user_id: int, provider: str) -> dict[str, Any] | None:
    """Returns the updated user row, or None if migration 015 hasn't been
    applied yet. Does NOT validate `provider` itself -- the enum column
    does that at the DB level (an invalid value raises, which the caller
    -- agents/registry.py's set_default_email_provider tool -- avoids by
    checking against {"messa", "gmail"} before ever calling this)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "default_email_provider"):
            return None
        row = await conn.fetchrow(
            "UPDATE users SET default_email_provider = $2 WHERE id = $1 RETURNING *",
            user_id, provider,
        )
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Personal-inbox email (migrations/013_personal_email.sql) -- Messa's own
# <local-part>@config.TEXTMESSA_EMAIL_DOMAIN address per user, distinct from
# both the Gmail connection block above and users.email (the user's own
# on-file contact address, migrations/003_user_email.sql). See
# tools/personal_inbox_tools.py and server.py's
# /webhooks/personal-email/inbound. Additive/no-op-safe like everything
# else in this file: all no-op (return None/True/[]) until migration 013 has
# been applied.
# ---------------------------------------------------------------------------

def _slugify_local_part(name: str | None, user_id: int) -> str:
    """Best-effort local-part from a display name ("Jane Doe" -> "janedoe"),
    falling back to "user<id>" for a still-nameless brand-new user (email
    provisioning runs on every context load, including before onboarding
    asks for a name -- see cli.load_user_context) or a name that's nothing
    but punctuation/emoji once stripped. Deliberately plain
    alphanumeric-only: an RFC 5322 local-part technically allows a lot more,
    but plenty of real mail providers choke on anything fancier, and this is
    meant to be easy to read aloud/type into a form, not maximally
    expressive."""
    base = re.sub(r"[^a-z0-9]+", "", (name or "").lower())[:24]
    return base if base else f"user{user_id}"


async def get_or_create_messa_email_local_part(user_id: int, name: str | None) -> str | None:
    """Idempotent: returns the existing local part if this user already has
    one, otherwise claims one now and persists it. Called on every
    cli.load_user_context (same "cheap after the first time" shape as
    get_or_create_live_share_token), so a user's address is ready before
    they ever ask for it.

    Collision handling: tries the clean slug first (e.g. "janedoe"), and
    only falls back to a decorated one ("janedoe482", using this user's own
    id) on an actual collision -- rather than pre-emptively checking and
    then racing another concurrent signup for the same name, the fallback
    itself (base + a unique user_id) can never collide, so one retry always
    succeeds without a loop."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "messa_email_local_part"):
            return None
        row = await conn.fetchrow("SELECT messa_email_local_part FROM users WHERE id = $1", user_id)
        if row is None:
            return None  # unknown user id -- nothing to provision
        if row["messa_email_local_part"]:
            return row["messa_email_local_part"]

        base = _slugify_local_part(name, user_id)
        for candidate in (base, f"{base}{user_id}"):
            try:
                await conn.execute(
                    "UPDATE users SET messa_email_local_part = $2 WHERE id = $1", user_id, candidate,
                )
                return candidate
            except asyncpg.UniqueViolationError:
                continue  # the clean slug was taken -- try the decorated fallback
        return None  # unreachable in practice: base+user_id is always unique


async def get_user_by_messa_email_local_part(local_part: str) -> dict[str, Any] | None:
    """Reverse lookup for server.py's inbound webhook: which user does
    <local_part>@TEXTMESSA_EMAIL_DOMAIN belong to. Case-insensitive (email
    local parts arrive in whatever case the sender's mail client used;
    ours are always assigned lowercase, so normalizing the lookup side is
    enough -- no need to touch what's stored)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "messa_email_local_part"):
            return None
        row = await conn.fetchrow(
            "SELECT * FROM users WHERE messa_email_local_part = $1", local_part.strip().lower(),
        )
        return dict(row) if row else None


async def record_inbound_personal_email(
    user_id: int, message_id: str | None, from_address: str, subject: str | None,
) -> bool:
    """True if this is a genuinely new inbound email; False if message_id
    was already recorded (a redelivered webhook, most likely -- see
    migrations/013_personal_email.sql's docstring) and the caller should
    skip reprocessing it. A missing/empty message_id (malformed mail, rare
    but real) always counts as new rather than being deduped against every
    other message_id-less email that ever arrives -- see the UNIQUE
    constraint this would otherwise collide against."""
    if not message_id:
        return True
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "inbound_personal_emails"):
            return True
        row = await conn.fetchrow(
            """
            INSERT INTO inbound_personal_emails (user_id, message_id, from_address, subject)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (message_id) DO NOTHING
            RETURNING id
            """,
            user_id, message_id, from_address, subject,
        )
        return row is not None


# ---------------------------------------------------------------------------
# Messa's own email: unified inbound+outbound thread log
# (migrations/014_messa_email_messages.sql). Superseded the dedup-only
# inbound_personal_emails table above for anything new -- that table is
# left alone (a few already-tested rows, no further writes), dedup for new
# inbound mail now happens against THIS table's message_id UNIQUE
# constraint instead (log_inbound_personal_email's ON CONFLICT). See
# tools/personal_inbox_tools.py for how Messa actually uses this (thread
# history recall, search, and reply_to_email looking up who to reply to).
# ---------------------------------------------------------------------------

def _compute_thread_id(message_id: str, in_reply_to: str | None, references: str | None) -> str:
    refs = (references or "").split()
    if refs:
        return refs[0]
    if in_reply_to and in_reply_to.strip():
        return in_reply_to.strip()
    return message_id


def _clean_subject(subject: str | None) -> str:
    if not subject:
        return ""
    cleaned = subject.strip()
    while True:
        lower = cleaned.lower()
        if lower.startswith("re:"):
            cleaned = cleaned[3:].strip()
        elif lower.startswith("fwd:"):
            cleaned = cleaned[4:].strip()
        else:
            break
    return cleaned.lower()


async def _resolve_thread_id(
    conn: asyncpg.Connection,
    user_id: int,
    message_id: str,
    in_reply_to: str | None,
    references: str | None,
    subject: str | None,
    counterpart_address: str,
) -> str:
    """Computes the thread_id via header inspection (_compute_thread_id),
    and if no existing row in messa_email_messages matches that thread_id directly,
    falls back to matching by normalized subject + counterpart address to handle
    cases where mail providers (e.g. Resend / SES) transform outbound Message-IDs."""
    primary_thread_id = _compute_thread_id(message_id, in_reply_to, references)
    
    # 1. Check if primary_thread_id already matches an existing thread
    existing = await conn.fetchval(
        "SELECT thread_id FROM messa_email_messages WHERE user_id = $1 AND (thread_id = $2 OR message_id = $2) LIMIT 1",
        user_id, primary_thread_id,
    )
    if existing:
        return existing

    # 2. Subject + counterpart fallback matching
    cleaned = _clean_subject(subject)
    if cleaned and counterpart_address:
        counterpart_pattern = f"%{counterpart_address.strip().lower()}%"
        fallback_thread = await conn.fetchval(
            """
            SELECT thread_id FROM messa_email_messages
            WHERE user_id = $1
              AND (LOWER(from_address) LIKE $2 OR LOWER(to_address) LIKE $2)
              AND LOWER(REGEXP_REPLACE(subject, '^(re|fwd):\\s*', '', 'gi')) = $3
            ORDER BY id DESC LIMIT 1
            """,
            user_id, counterpart_pattern, cleaned,
        )
        if fallback_thread:
            return fallback_thread

    return primary_thread_id


def _generate_message_id(domain: str) -> str:
    """A fresh RFC 5322-shaped Message-ID for a message that arrived
    without a usable one (rare -- malformed mail) or that we're about to
    send ourselves (channels/resend.py calls this directly, not through
    log_inbound_personal_email)."""
    return f"<{uuid.uuid4()}@{domain}>"


async def log_inbound_personal_email(
    user_id: int,
    message_id: str | None,
    from_address: str,
    to_address: str,
    subject: str | None,
    body_text: str | None,
    in_reply_to: str | None = None,
    references: str | None = None,
    raw_json: str | None = None,
    attachment_filename: str | None = None,
) -> dict[str, Any] | None:
    """Durable write + dedup in one step: ON CONFLICT (message_id) DO
    NOTHING means a redelivered webhook (see migrations/014's docstring)
    returns None here instead of creating a second row or re-triggering a
    turn -- server.py's webhook route uses this same call for both
    persistence and the dedup check, rather than two separate steps.

    A missing message_id (malformed mail, rare but real) gets a generated
    one instead of being stored as NULL/empty -- the column is UNIQUE, so
    every such message needs its own value or the second one would
    silently collide and look like a duplicate of the first.

    `attachment_filename` (migrations/016_messa_email_attachment.sql):
    previously written only by log_outbound_personal_email below -- now
    also set here when server.py's webhook found a PDF attachment on the
    inbound message (see cloudflare/personal-email-worker/worker.js's
    pdf_attachments forwarding + pdf_reader.py), so the paperclip note in
    _format_message_line/the live-view Emails page shows up for inbound
    mail too, not just outbound. None (unchanged default) for a plain
    email with nothing attached.

    Returns the inserted row (including its computed thread_id) for a
    genuinely new message; None for a duplicate, or if migration 014 hasn't
    been applied yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return None
        real_message_id = message_id.strip() if message_id and message_id.strip() else _generate_message_id(
            to_address.split("@", 1)[-1] or config.TEXTMESSA_EMAIL_DOMAIN
        )
        thread_id = await _resolve_thread_id(
            conn, user_id, real_message_id, in_reply_to, references, subject, from_address
        )
        has_attachment_col = await _has_column(conn, "messa_email_messages", "attachment_filename")
        if has_attachment_col:
            row = await conn.fetchrow(
                """
                INSERT INTO messa_email_messages
                    (user_id, direction, thread_id, message_id, in_reply_to, references_header,
                     from_address, to_address, subject, body_text, raw_json, attachment_filename)
                VALUES ($1, 'inbound', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING *
                """,
                user_id, thread_id, real_message_id, in_reply_to, references,
                from_address, to_address, subject, body_text, raw_json, attachment_filename,
            )
        else:
            # migration 016 not applied yet -- degrade gracefully rather
            # than erroring: the email itself is still worth logging even
            # if we can't note that it had an attachment.
            row = await conn.fetchrow(
                """
                INSERT INTO messa_email_messages
                    (user_id, direction, thread_id, message_id, in_reply_to, references_header,
                     from_address, to_address, subject, body_text, raw_json)
                VALUES ($1, 'inbound', $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING *
                """,
                user_id, thread_id, real_message_id, in_reply_to, references,
                from_address, to_address, subject, body_text, raw_json,
            )
        return dict(row) if row else None


async def log_outbound_personal_email(
    user_id: int,
    message_id: str,
    from_address: str,
    to_address: str,
    subject: str | None,
    body_text: str | None,
    in_reply_to: str | None = None,
    references: str | None = None,
    raw_json: str | None = None,
    sent_autonomously: bool = False,
    attachment_filename: str | None = None,
) -> dict[str, Any] | None:
    """Called by channels/resend.py after every successful send -- the
    single choke point for outbound logging, so nothing that goes through
    resend.send_email can be forgotten regardless of which tool/future
    caller invoked it. `message_id` here is always the value resend.py
    itself generated and set as the outbound Message-ID header (not
    anything parsed back out of Resend's API response -- see that module's
    docstring for why). No-ops (returns None) pre-migration, same as the
    inbound counterpart.

    `attachment_filename` (migrations/016_messa_email_attachment.sql):
    just the filename Messa attached, e.g. "invoice.pdf" -- None for a
    plain send/reply with nothing attached. Column write is unconditional
    here (INSERT always includes it, defaulting to whatever NULL Postgres
    returns for an un-migrated column read elsewhere would need its own
    guard, but writing a value to a column that doesn't exist yet would
    itself fail -- see the _has_column guard immediately below for why
    that's safe: this whole function already no-ops before migration 014,
    and 016 ships alongside/after 014 in the same additive sequence)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return None
        has_attachment_col = await _has_column(conn, "messa_email_messages", "attachment_filename")
        thread_id = await _resolve_thread_id(
            conn, user_id, message_id, in_reply_to, references, subject, to_address
        )
        if has_attachment_col:
            row = await conn.fetchrow(
                """
                INSERT INTO messa_email_messages
                    (user_id, direction, thread_id, message_id, in_reply_to, references_header,
                     from_address, to_address, subject, body_text, raw_json, sent_autonomously,
                     attachment_filename)
                VALUES ($1, 'outbound', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                RETURNING *
                """,
                user_id, thread_id, message_id, in_reply_to, references,
                from_address, to_address, subject, body_text, raw_json, sent_autonomously,
                attachment_filename,
            )
        else:
            # migration 016 not applied yet -- degrade gracefully rather
            # than erroring: the send itself already succeeded by the time
            # this is called, so failing to log the attachment's filename
            # must never look like the whole send failed.
            row = await conn.fetchrow(
                """
                INSERT INTO messa_email_messages
                    (user_id, direction, thread_id, message_id, in_reply_to, references_header,
                     from_address, to_address, subject, body_text, raw_json, sent_autonomously)
                VALUES ($1, 'outbound', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
                RETURNING *
                """,
                user_id, thread_id, message_id, in_reply_to, references,
                from_address, to_address, subject, body_text, raw_json, sent_autonomously,
            )
        return dict(row) if row else None


async def get_thread_messages(user_id: int, thread_id: str) -> list[dict[str, Any]]:
    """Every message (both directions) in one thread, oldest first -- the
    full conversation view personal_inbox_tools.py's get_thread_history
    renders for Messa."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return []
        rows = await conn.fetch(
            """
            SELECT * FROM messa_email_messages
            WHERE user_id = $1 AND thread_id = $2
            ORDER BY created_at ASC
            """,
            user_id, thread_id,
        )
        return _rows(rows)


async def find_thread_id_for_counterpart(user_id: int, counterpart_address: str) -> str | None:
    """The most recent thread_id involving this external address, checked
    on both sides (they emailed the user, or the user/Messa emailed them) --
    what get_thread_history resolves "the thread with support@brand.com"
    into before calling get_thread_messages. None if there's no history
    with that address at all."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return None
        row = await conn.fetchrow(
            """
            SELECT thread_id FROM messa_email_messages
            WHERE user_id = $1 AND (from_address ILIKE $2 OR to_address ILIKE $2)
            ORDER BY created_at DESC LIMIT 1
            """,
            user_id, counterpart_address.strip(),
        )
        return row["thread_id"] if row else None


async def get_latest_inbound_message_in_thread(user_id: int, thread_id: str) -> dict[str, Any] | None:
    """The most recent INBOUND row in this thread -- what reply_to_email
    resolves who-to-reply-to (from_address), the subject, and the
    Message-ID/References to thread a reply under, from. None if this
    thread has no inbound messages at all (a thread Messa/the user started
    outbound and nobody's replied to yet -- nothing to reply TO)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return None
        row = await conn.fetchrow(
            """
            SELECT * FROM messa_email_messages
            WHERE user_id = $1 AND thread_id = $2 AND direction = 'inbound'
            ORDER BY created_at DESC LIMIT 1
            """,
            user_id, thread_id,
        )
        return dict(row) if row else None


async def search_personal_emails(
    user_id: int,
    query: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    direction: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Free-text (subject/body ILIKE) plus an optional [since, until) time
    range, most recent first -- the backing query for search_my_emails'
    "did anything come in this morning after 9am" style asks. `since`/
    `until` are already-resolved tz-aware datetimes (the tool itself runs
    the model's date phrase through timeutil.to_local_aware before calling
    this, same pattern as every other date-taking tool in this codebase).

    `direction` ('inbound' or 'outbound', None for both) and `offset` were
    added for the live-view Emails dashboard page (server.py's
    /live/<token>/emails route) -- its Inbox/Sent tabs and "load more"
    pagination -- but are equally usable by search_my_emails itself later;
    both default to their old no-filter/no-offset behavior so every
    existing caller (the tool, and the tests written against it) is
    unaffected."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return []
        conditions = ["user_id = $1"]
        args: list[Any] = [user_id]
        if query:
            args.append(f"%{query}%")
            conditions.append(f"(subject ILIKE ${len(args)} OR body_text ILIKE ${len(args)})")
        if since is not None:
            args.append(since)
            conditions.append(f"created_at >= ${len(args)}")
        if until is not None:
            args.append(until)
            conditions.append(f"created_at < ${len(args)}")
        if direction in ("inbound", "outbound"):
            args.append(direction)
            conditions.append(f"direction = ${len(args)}")
        args.append(limit)
        limit_idx = len(args)
        args.append(offset)
        offset_idx = len(args)
        rows = await conn.fetch(
            f"""
            SELECT * FROM messa_email_messages
            WHERE {' AND '.join(conditions)}
            ORDER BY created_at DESC LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *args,
        )
        return _rows(rows)


async def list_email_threads(
    user_id: int,
    direction: str | None = None,
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """One row per THREAD (not per message) -- the whole conversation's own
    latest message, regardless of that message's own direction -- for the
    live-view Emails page's Inbox/Sent list (server.py's /live/<token>/emails
    route). Per explicit product feedback: showing one row per individual
    message split a single back-and-forth conversation into fragments
    scattered across both tabs, which read as confusing next to a real mail
    app's behavior (Gmail-style: a conversation is one row, wherever it
    shows up, always previewing whatever was said last in it).

    `direction` here means "include this thread if EITHER endpoint has ever
    used it in that direction" -- 'inbound' for Inbox (any thread with at
    least one message that arrived), 'outbound' for Sent (any thread with
    at least one message you/Messa sent) -- NOT "only show messages of this
    direction". A thread with both directions (the common case once
    there's been a reply) legitimately appears in both tabs, each time
    previewing the SAME latest message regardless of which tab it's shown
    under -- exactly like Gmail shows the same conversation under both
    Inbox and Sent once you've replied to it. `query` matches if ANY
    message anywhere in the thread's subject/body matches, not just the
    previewed one -- finding a thread by something said earlier in it, even
    if the latest reply does't mention it, is expected mail-app behavior.

    Implemented as one query: filter down to qualifying threads first (via
    membership subqueries against thread_id, so filtering never restricts
    away messages from other parts of the SAME qualifying thread), then
    pick each thread's single latest row with ROW_NUMBER() OVER (PARTITION
    BY thread_id ORDER BY created_at DESC), then page over just those
    picked rows -- no separate query for "list threads" vs "get the latest
    message in each"."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return []
        conditions = ["user_id = $1"]
        args: list[Any] = [user_id]
        if direction in ("inbound", "outbound"):
            args.append(direction)
            conditions.append(
                f"thread_id IN (SELECT thread_id FROM messa_email_messages "
                f"WHERE user_id = $1 AND direction = ${len(args)})"
            )
        if query:
            args.append(f"%{query}%")
            q_idx = len(args)
            conditions.append(
                f"thread_id IN (SELECT thread_id FROM messa_email_messages "
                f"WHERE user_id = $1 AND (subject ILIKE ${q_idx} OR body_text ILIKE ${q_idx}))"
            )
        args.append(limit)
        limit_idx = len(args)
        args.append(offset)
        offset_idx = len(args)
        rows = await conn.fetch(
            f"""
            SELECT * FROM (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY created_at DESC) AS rn
                FROM messa_email_messages
                WHERE {' AND '.join(conditions)}
            ) latest_per_thread
            WHERE rn = 1
            ORDER BY created_at DESC
            LIMIT ${limit_idx} OFFSET ${offset_idx}
            """,
            *args,
        )
        return _rows(rows)


# ---------------------------------------------------------------------------
# Generated-document shares (migrations/018_generated_document_shares.sql):
# a public, unguessable-token URL for a file Messa generated, so it can be
# attached to an outbound TEXT message via Sendblue's media_url (which
# needs a URL it can fetch, not raw bytes -- see agents/registry.py's
# send_pdf_over_text and server.py's GET /files/{token}).
# ---------------------------------------------------------------------------

async def create_document_share(user_id: int, file_path: str, filename: str) -> str | None:
    """Mints a fresh unguessable token for `file_path` and records it.
    Same token-generation approach as get_or_create_live_share_token
    (secrets.token_urlsafe) -- not sequential/enumerable. Returns None
    (no-op) if migration 018 hasn't been applied yet, same pattern as
    every other optional-table function in this module."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "generated_document_shares"):
            return None
        token = secrets.token_urlsafe(24)
        await conn.execute(
            "INSERT INTO generated_document_shares (user_id, token, file_path, filename) "
            "VALUES ($1, $2, $3, $4)",
            user_id, token, file_path, filename,
        )
        return token


async def get_document_share_by_token(token: str) -> dict[str, Any] | None:
    """Looked up by server.py's public GET /files/{token} route -- that
    route re-validates file_path against config.OUTPUTS_DIR itself before
    serving anything (see config.resolve_output_file), this function is
    just the lookup."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "generated_document_shares"):
            return None
        row = await conn.fetchrow(
            "SELECT * FROM generated_document_shares WHERE token = $1", token,
        )
        return dict(row) if row else None


# ---------------------------------------------------------------------------
# Usage limits / subscription plans (migrations/023_usage_limits.sql,
# messa/plans.py, messa/usage.py). One row per user per feature per day;
# `day` is the CALLER's responsibility to compute in the user's own
# timezone (see timeutil.local_today) -- this module just stores whatever
# date it's given.
# ---------------------------------------------------------------------------

async def check_and_increment_usage(
    user_id: int, feature: str, day: Any, limit: int | None, amount: int = 1,
) -> tuple[bool, int]:
    """Atomically increments usage_daily_counts(user_id, feature, day) by
    `amount` and reports whether the count after incrementing is still
    within `limit` (None = always allowed -- still increments, for cost
    visibility even on an unlimited plan/feature). One
    INSERT ... ON CONFLICT ... RETURNING statement, so two concurrent tool
    calls in the same turn (e.g. Messa sending two texts back to back)
    can't both read a stale count and both slip past the cap -- Postgres
    itself serializes the upsert.

    Deliberately always increments, even on the call that pushes the count
    past `limit`: under real concurrency, a couple of calls landing at
    exactly the boundary could all get counted before any of them observes
    `allowed=False`. Accepted on purpose -- this is a soft, conversational
    daily cap, not a hard security boundary, and always incrementing keeps
    the stored count an honest record of what actually happened rather
    than an undercount that stops the moment the limit is hit.

    Returns (True, 0) if migration 023 hasn't been applied yet to this
    deployment, OR if anything about reaching the database itself fails
    (pool creation, a dropped connection, ...) -- fails OPEN (nothing is
    ever blocked) rather than a DB hiccup on this one counter table taking
    down every metered tool call across the whole app. Logged, not
    silent -- a real outage should be visible in the console even though
    it doesn't block anyone."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if not await _has_table(conn, "usage_daily_counts"):
                return True, 0
            row = await conn.fetchrow(
                """
                INSERT INTO usage_daily_counts (user_id, feature, day, count, updated_at)
                VALUES ($1, $2, $3, $4, NOW())
                ON CONFLICT (user_id, feature, day)
                DO UPDATE SET count = usage_daily_counts.count + $4, updated_at = NOW()
                RETURNING count
                """,
                user_id, feature, day, amount,
            )
    except Exception as e:  # noqa: BLE001 - usage metering must never break a real tool call
        console.system(f"check_and_increment_usage: DB unavailable, failing open ({feature}): {e}")
        return True, 0
    count_after = row["count"]
    allowed = limit is None or count_after <= limit
    return allowed, count_after


async def get_usage_count(user_id: int, feature: str, day: Any) -> int:
    """Read-only peek at today's count so far, with no increment -- used
    for a pre-agent-run gate (cli.run_message) that needs to know "is this
    user already over their limit" before deciding whether to even start
    an agent loop, without itself counting as a use. Returns 0 for a
    missing row, a missing table, or any DB-reachability failure (same
    fail-open reasoning as check_and_increment_usage)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            if not await _has_table(conn, "usage_daily_counts"):
                return 0
            row = await conn.fetchrow(
                "SELECT count FROM usage_daily_counts WHERE user_id = $1 AND feature = $2 AND day = $3",
                user_id, feature, day,
            )
            return row["count"] if row else 0
    except Exception as e:  # noqa: BLE001 - usage metering must never break a real tool call
        console.system(f"get_usage_count: DB unavailable, failing open ({feature}): {e}")
        return 0
