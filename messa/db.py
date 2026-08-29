"""Async data access layer over the Neon Postgres database.

One asyncpg pool, thin repository functions grouped by table. Nothing here
knows about LangChain/agents — tools in `messa/tools/*` call these functions
and translate results into tool-call strings.

`payload` columns are TEXT (JSON-serialized), matching the schema exactly
(not JSONB), so we json.dumps/json.loads by hand at the boundary.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from . import config, timeutil

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

ONBOARDING_STEPS = ["awaiting_name", "awaiting_email", "awaiting_location", "complete"]


def _initial_onboarding_step(name: str | None) -> str:
    """Where a brand-new user's onboarding starts, given what we already know."""
    return "awaiting_email" if name else "awaiting_name"


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


async def update_user_timezone(user_id: int, timezone_name: str, confirmed: bool = True) -> dict[str, Any] | None:
    """Overwrite a user's stored timezone -- the one place, other than
    account creation, that users.timezone is ever written. `confirmed`
    tracks whether this came from an actually-resolved city/zip (see
    timeutil.resolve_timezone) as opposed to still being the
    config.DEFAULT_TIMEZONE placeholder. Additive/no-op-safe: if
    migrations/007_timezone_confirmed.sql hasn't been applied yet, still
    updates the timezone itself, just without the confirmed flag."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if await _has_column(conn, "users", "timezone_confirmed"):
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
    updated = await update_user_timezone(user_row["id"], resolution.timezone, confirmed=resolution.confident)
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
    """Save one onboarding field (name/email/city) and advance onboarding_step.

    `value` may be the literal string 'skip' for the optional email step --
    that still advances onboarding without writing anything to the column.

    For `field == "city"`, this also resolves and stores the user's real
    timezone (via timeutil.resolve_timezone) instead of leaving it on
    whatever config.DEFAULT_TIMEZONE was written at account creation -- the
    root fix for a user's stored timezone never actually reflecting where
    they live. The returned dict's `timezone_confirmed` tells the caller
    (agents/registry.py's save_profile_info tool) whether that resolution
    was unambiguous; when it's False, the caller should ask the user for a
    zip code or "city, state" instead of trusting a guess.
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
        step_for_field = {"name": "awaiting_name", "email": "awaiting_email", "city": "awaiting_location"}[field]
        if current_step == step_for_field:
            next_step = ONBOARDING_STEPS[ONBOARDING_STEPS.index(step_for_field) + 1]
            await conn.execute("UPDATE users SET onboarding_step = $2 WHERE id = $1", user_id, next_step)
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

    if field == "city" and value and value.strip().lower() != "skip":
        resolution = await timeutil.resolve_timezone(value.strip())
        if resolution is not None:
            updated = await update_user_timezone(user_id, resolution.timezone, confirmed=resolution.confident)
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
) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tasks (user_id, title, description, due_date, priority)
            VALUES ($1, $2, $3, $4, COALESCE($5::task_priority, 'medium'))
            RETURNING *
            """,
            user_id, title, description, due_date, priority,
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
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE reminders SET status = 'sent' WHERE id = $1", reminder_id)


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
    row = await conn.fetchrow(
        """
        INSERT INTO calendar_events (user_id, title, start_time, end_time, location, notes)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
        """,
        user_id, payload["title"],
        _parse_dt(payload["start_time"], user_tz), _parse_dt(payload["end_time"], user_tz),
        payload.get("location"), payload.get("notes"),
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
    user_id: int, name: str, relationship_type: str | None = None, notes: str | None = None
) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
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


async def get_due_cron_jobs_for_delivery(now: datetime | None = None) -> list[dict[str, Any]]:
    """Same as get_due_cron_jobs, but joined with users for the phone_number
    a production re-invocation needs -- used by server.py's real poller."""
    now = now or datetime.now(timezone.utc)
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.*, u.phone_number
            FROM cron_jobs c JOIN users u ON u.id = c.user_id
            WHERE c.status = 'active' AND c.next_run_at <= $1
            """,
            now,
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
        INSERT INTO cron_jobs (user_id, prompt_or_task, cron_expression, user_timezone, next_run_at)
        VALUES ($1, $2, $3, $4, $5) RETURNING *
        """,
        user_id, payload["prompt_or_task"], payload["cron_expression"],
        payload.get("user_timezone", config.DEFAULT_TIMEZONE), payload["next_run_at"],
    )
    return dict(row)


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
