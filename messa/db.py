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

from . import config

_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(config.DATABASE_URL, min_size=1, max_size=5)
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


def _parse_dt(value: Any) -> datetime | None:
    """asyncpg needs real datetime objects for timestamptz columns -- it won't
    parse ISO strings itself (unlike psycopg2). Tool-proposed payloads carry
    ISO-8601 strings (that's what the LLM produces and what JSON can store),
    so every applier that touches a timestamptz column runs values through
    this first."""
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass

    # Relative/human date parsing fallback
    now = datetime.now(timezone.utc)
    lower = text.lower()
    if lower == "today":
        return now
    elif lower == "tomorrow":
        return now + timedelta(days=1)

    try:
        from dateutil import parser
        base = now
        if "tomorrow" in lower:
            base = now + timedelta(days=1)
            text_clean = lower.replace("tomorrow", "").replace("noon", "12:00 PM").strip()
            dt = parser.parse(text_clean, default=base) if text_clean else base
        else:
            text_clean = lower.replace("today", "").replace("noon", "12:00 PM").strip()
            dt = parser.parse(text_clean, default=base)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        raise ValueError(f"Could not parse datetime string: {value!r}")


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
            "SELECT live_view_url, live_view_task, live_view_started_at, name "
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
        }


async def save_profile_field(user_id: int, field: str, value: str) -> dict[str, Any]:
    """Save one onboarding field (name/email/city) and advance onboarding_step.

    `value` may be the literal string 'skip' for the optional email step --
    that still advances onboarding without writing anything to the column.
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
    key = (table, column)
    if key not in _column_cache:
        exists = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = $1 AND column_name = $2
            )
            """,
            table, column,
        )
        _column_cache[key] = bool(exists)
    return _column_cache[key]


# ---------------------------------------------------------------------------
# Tasks (reads are direct; writes are gated -- see pending_actions below)
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


async def _insert_task(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO tasks (user_id, title, description, due_date, priority)
        VALUES ($1, $2, $3, $4, COALESCE($5::task_priority, 'medium'))
        RETURNING *
        """,
        user_id,
        payload["title"],
        payload.get("description"),
        _parse_dt(payload.get("due_date")),
        payload.get("priority"),
    )
    return dict(row)


async def _update_task(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
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
        payload["task_id"], user_id,
        payload.get("title"), payload.get("description"), _parse_dt(payload.get("due_date")),
        payload.get("status"), payload.get("priority"),
    )
    return dict(row) if row else {}


async def _delete_task(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        "DELETE FROM tasks WHERE id = $1 AND user_id = $2 RETURNING id",
        payload["task_id"], user_id,
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


async def mark_reminder_sent(reminder_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE reminders SET status = 'sent' WHERE id = $1", reminder_id)


async def _insert_reminder(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        """
        INSERT INTO reminders (user_id, trigger_time, message, status, checkin_for_task_id)
        VALUES ($1, $2, $3, COALESCE($4::reminder_status, 'pending'::reminder_status), $5) RETURNING *
        """,
        user_id,
        _parse_dt(payload["trigger_time"]),
        payload["message"],
        payload.get("status"),
        payload.get("checkin_for_task_id"),
    )
    return dict(row)


async def _cancel_reminder(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        "UPDATE reminders SET status = 'cancelled' WHERE id = $1 AND user_id = $2 RETURNING id",
        payload["reminder_id"], user_id,
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
    row = await conn.fetchrow(
        """
        INSERT INTO calendar_events (user_id, title, start_time, end_time, location, notes)
        VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
        """,
        user_id, payload["title"], _parse_dt(payload["start_time"]), _parse_dt(payload["end_time"]),
        payload.get("location"), payload.get("notes"),
    )
    return dict(row)


async def _update_calendar_event(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
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
        payload["event_id"], user_id, payload.get("title"), _parse_dt(payload.get("start_time")),
        _parse_dt(payload.get("end_time")), payload.get("location"), payload.get("notes"), payload.get("status"),
    )
    return dict(row) if row else {}


async def _delete_calendar_event(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        "UPDATE calendar_events SET status = 'cancelled' WHERE id = $1 AND user_id = $2 RETURNING id",
        payload["event_id"], user_id,
    )
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# Notes (create is direct/ungated; delete is gated -- matches action_type enum)
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


async def _delete_note(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    row = await conn.fetchrow(
        "DELETE FROM notes WHERE id = $1 AND user_id = $2 RETURNING id",
        payload["note_id"], user_id,
    )
    return dict(row) if row else {}


# ---------------------------------------------------------------------------
# People / contacts (ungated -- not in the action_type enum)
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
    row = await conn.fetchrow(
        """
        INSERT INTO cron_jobs (user_id, prompt_or_task, cron_expression, user_timezone, next_run_at)
        VALUES ($1, $2, $3, $4, $5) RETURNING *
        """,
        user_id, payload["prompt_or_task"], payload["cron_expression"],
        payload.get("user_timezone", config.DEFAULT_TIMEZONE), _parse_dt(payload["next_run_at"]),
    )
    return dict(row)


# ---------------------------------------------------------------------------
# Pending actions -- the confirm-before-write gate.
#
# The DB's action_type enum defines exactly which mutations require
# confirmation: create/update/delete task, create reminder/cancel reminder,
# create/update/delete calendar event, delete note, create recurring cron.
# Anything not in that enum (reads, notes creation, contacts) writes directly.
# ---------------------------------------------------------------------------

_APPLIERS = {
    "create_task": _insert_task,
    "update_task": _update_task,
    "delete_task": _delete_task,
    "create_reminder": _insert_reminder,
    "cancel_reminder": _cancel_reminder,
    "create_calendar_event": _insert_calendar_event,
    "update_calendar_event": _update_calendar_event,
    "delete_calendar_event": _delete_calendar_event,
    "delete_note": _delete_note,
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
