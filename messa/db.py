"""Async data access layer over the Neon Postgres database.

One asyncpg pool, thin repository functions grouped by table. Nothing here
knows about LangChain/agents — tools in `messa/tools/*` call these functions
and translate results into tool-call strings.

`payload` columns are TEXT (JSON-serialized), matching the schema exactly
(not JSONB), so we json.dumps/json.loads by hand at the boundary.
"""
from __future__ import annotations

import json
import mimetypes
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
        # Pool size and command_timeout are configurable (MESSA_DB_POOL_MIN_SIZE
        # / MESSA_DB_POOL_MAX_SIZE / MESSA_DB_COMMAND_TIMEOUT_SECONDS, see
        # config.py) rather than hardcoded -- launch-scale traffic needs more
        # headroom than the original min=1/max=5 gave us, and PgBouncer's
        # transaction pooling in front of this (see the statement_cache_size
        # comment above) means a bigger asyncpg-side pool multiplexes safely
        # onto Neon's actual backend connections instead of each of these 20
        # holding open its own dedicated Postgres backend. command_timeout
        # guards against a single slow/stuck query holding a pool connection
        # (and therefore a request) open indefinitely under load.
        _pool = await asyncpg.create_pool(
            config.DATABASE_URL,
            min_size=config.DB_POOL_MIN_SIZE,
            max_size=config.DB_POOL_MAX_SIZE,
            statement_cache_size=0,
            command_timeout=config.DB_COMMAND_TIMEOUT_SECONDS,
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
    "awaiting_name", "complete",
]
# Name is the ONLY real onboarding gate now -- matches the "onboarded in a
# single text" promise, and gets the user to Messa's own onboarding-complete
# reveal (her email address + live-view link, see cli.py's
# _onboarding_complete_messages) as fast as possible. City and email used to
# be sequential gated steps here (awaiting_location -> awaiting_email ->
# complete) -- removed by request: forcing two more back-and-forth answers
# before onboarding could ever "complete" both contradicted the single-text
# promise and, worse, meant the model reliably dropped the ask altogether
# (a two-sentence step instruction buried in a long system prompt loses to
# the user's actual question almost every time) which is exactly the bug
# that prompted this change. City and email are still valuable and still
# asked -- just conversationally, any time, tied to a real moment they're
# actually useful for (see agents/registry.py's profile-enrichment prompt
# block), the same way connecting Gmail already worked before this change
# (see the now-removed "awaiting_email_connect" step's history below).
# `save_profile_field` reflects this: only field == "name" still advances
# onboarding_step; city/email write their columns unconditionally,
# regardless of onboarding_step, since there's no step left for them to be
# gated on.
#
# (Historical note, kept for context: there used to be a 4th step here,
# "awaiting_email_connect", an explicit "want me to connect your Gmail?"
# yes/no gate before onboarding could reach "complete" -- removed even
# earlier than this change, for the same underlying reason: it added a full
# extra back-and-forth before the user ever saw what Messa actually is, for
# a capability Messa can already offer conversationally any time ("connect
# my gmail") -- see agents/registry.py's `known_str`.)


def _initial_onboarding_step(name: str | None) -> str:
    """Where a brand-new user's onboarding starts, given what we already know."""
    return "complete" if name else "awaiting_name"


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


async def get_user_by_phone(phone_number: str) -> dict[str, Any] | None:
    """Read-only lookup by phone number -- unlike get_or_create_user, NEVER
    creates a row. This is exactly the distinction messa/waitlist.py's gate
    needs: "does this inbound text belong to someone who already exists"
    without the side effect of creating them as a side effect of checking."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE phone_number = $1", phone_number)
        return dict(row) if row else None


async def count_all_users() -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users")


async def get_all_user_phone_numbers() -> list[str]:
    """Every user's phone number -- the recipient list for a broadcast (all
    users, unfiltered, per the current product scope; a future round can
    narrow this by region/plan/active-only without changing the broadcast
    engine itself)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT phone_number FROM users")
        return [r["phone_number"] for r in rows]


async def get_all_user_ids() -> list[int]:
    """Every user id -- the iteration list for the daily memory batch job
    (messa/memory.py's run_daily_memory_batch), same "all users, unfiltered"
    scope as get_all_user_phone_numbers above for the same reason (only 5
    users today; narrow this later if it ever needs to)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM users")
        return [r["id"] for r in rows]


# ---------------------------------------------------------------------------
# New-user cap + waitlist (migrations/025_new_user_cap_waitlist.sql,
# messa/waitlist.py, messa/tools/admin_tools.py). Every function here is a
# no-op-safe plain data operation -- the actual "should this person be let
# in right now" decision lives in messa/waitlist.py, not here, same
# db.py-stays-thin boundary as everywhere else in this file.
# ---------------------------------------------------------------------------

async def get_new_user_baseline_id() -> int:
    """Auto-captured exactly once: the highest users.id that already existed
    the moment the new-user cap first got checked for real (i.e. the first
    time MESSA_NEW_USER_CAP was set above 0 and someone new texted in) --
    stored in app_settings so it survives restarts and is shared across
    however many server instances are running. Every user with an id at or
    below this baseline is permanently grandfathered in, regardless of the
    cap; "new user" only ever means someone who signed up after this point.

    Race-safe: ON CONFLICT DO NOTHING means two concurrent first-callers
    can't produce two different baselines -- both re-read after their own
    insert attempt and return whichever value actually won."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM app_settings WHERE key = 'new_user_baseline_id'")
        if row:
            return int(row["value"])
        current_max = await conn.fetchval("SELECT COALESCE(MAX(id), 0) FROM users")
        await conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES ('new_user_baseline_id', $1)
            ON CONFLICT (key) DO NOTHING
            """,
            str(current_max),
        )
        row2 = await conn.fetchrow("SELECT value FROM app_settings WHERE key = 'new_user_baseline_id'")
        return int(row2["value"]) if row2 else current_max


async def count_new_users_since_baseline() -> int:
    baseline = await get_new_user_baseline_id()
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval("SELECT COUNT(*) FROM users WHERE id > $1", baseline)


async def get_new_user_cap_summary() -> dict[str, int]:
    """Admin-facing snapshot (admin_tools.py's get_new_user_cap_status):
    cap=0 means the cap isn't enabled, in which case `count` is reported as
    0 rather than actually queried -- there's no baseline to have been
    captured yet if the cap has never been checked for real."""
    cap = config.NEW_USER_CAP
    count = await count_new_users_since_baseline() if cap > 0 else 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        waitlist_count = await conn.fetchval("SELECT COUNT(*) FROM waitlist WHERE admitted_at IS NULL")
    return {"cap": cap, "count": count, "waitlist_count": waitlist_count or 0}


async def get_waitlist_entry(phone_number: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM waitlist WHERE phone_number = $1", phone_number)
        return dict(row) if row else None


async def add_to_waitlist(phone_number: str) -> dict[str, Any]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO waitlist (phone_number) VALUES ($1)
            ON CONFLICT (phone_number) DO NOTHING RETURNING *
            """,
            phone_number,
        )
        if row:
            return dict(row)
        existing = await conn.fetchrow("SELECT * FROM waitlist WHERE phone_number = $1", phone_number)
        return dict(existing) if existing else {}


async def mark_waitlist_notified(phone_number: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE waitlist SET notified_at = NOW() WHERE phone_number = $1", phone_number)


async def admit_from_waitlist(phone_number: str) -> dict[str, Any] | None:
    """Sets admitted_at -- doesn't create their users row itself (that still
    only happens through the normal get_or_create_user path), just tells
    messa/waitlist.py's gate to let their NEXT inbound text all the way
    through instead of re-waitlisting them, even if the cap is still full."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE waitlist SET admitted_at = NOW() WHERE phone_number = $1 RETURNING *",
            phone_number,
        )
        return dict(row) if row else None


async def list_waitlist() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM waitlist ORDER BY requested_at")
        return _rows(rows)


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
    looks like a normal idle state).

    Deliberately does NOT fall back to searching message_history for a
    user who was once texted this exact token, as a proposed fix for a
    hallucinated-live-link bug considered doing (see cli.py's
    _sanitize_live_view_urls for the actual fix that shipped instead).
    That fallback would have taken `token` -- straight from the public,
    unauthenticated URL path, with no charset restriction anywhere in the
    route -- into a LIKE '%' || token || '%' pattern: since LIKE treats an
    unescaped '%'/'_' inside the value as a real wildcard regardless of
    parameterization, a request for a token containing one (e.g. a single
    literal '%', percent-encoded as %25 in the URL) would match every row
    and resolve to whichever user Messa most recently replied to -- handing
    an anonymous visitor a real stranger's live browsing session. Rejected
    on review; a dead/wrong link 404ing is the correct, safe behavior a
    hallucinated token should have had all along, not a bug to soften."""
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
    state" reasoning as get_live_status_by_token. Also gates the
    /live/<token>/dashboard, /emails, and /emails/thread routes, so an
    unknown token here means no tasks/reminders/schedule/contacts/email
    content leaks either -- see get_live_status_by_token's own docstring
    for the message_history-fallback approach that was considered and
    rejected for exactly this function, for the same reason."""
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
    """Save one profile field (name/city/email).

    Only `field == "name"` still gates onboarding_step (awaiting_name ->
    complete, see ONBOARDING_STEPS) -- city and email are no longer
    sequential onboarding steps at all (see that constant's own comment for
    why), so they write their column any time, at any onboarding_step,
    conversationally, whenever the user actually gives one.

    `value` may be the literal string 'skip' for city or email -- nothing
    gets written, but migrations/031_profile_prompt_skips.sql's
    city_prompt_skipped/email_prompt_skipped flag is set instead (when that
    migration's applied; a no-op pre-migration, same graceful-degrade shape
    as everywhere else in this file), so agents/registry.py's
    profile-enrichment prompt block knows not to keep suggesting a field the
    user already explicitly declined. Skipping 'name' just does nothing
    (name isn't skippable -- there's no flag for it and it's the one thing
    onboarding actually needs to complete).

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

    is_skip = bool(value) and value.strip().lower() == "skip"
    skip_flag_column = {"city": "city_prompt_skipped", "email": "email_prompt_skipped"}.get(field)

    pool = await get_pool()
    async with pool.acquire() as conn:
        if is_skip and skip_flag_column:
            if await _has_column(conn, "users", skip_flag_column):
                await conn.execute(f"UPDATE users SET {skip_flag_column} = TRUE WHERE id = $1", user_id)
        elif field == "email" and not await _has_column(conn, "users", "email"):
            # Migration 003 not applied yet -- skip storing.
            pass
        elif value and not is_skip:
            await conn.execute(f"UPDATE users SET {field} = $2 WHERE id = $1", user_id, value.strip())

        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

        if field == "name":
            current_step = row["onboarding_step"]
            if current_step == "awaiting_name":
                next_step = ONBOARDING_STEPS[ONBOARDING_STEPS.index("awaiting_name") + 1]
                await conn.execute("UPDATE users SET onboarding_step = $2 WHERE id = $1", user_id, next_step)
                row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)

    if field == "name" and value and value.strip().lower() != "skip":
        # User just gave their name! Provision their <username>@textmessa.com address immediately.
        await get_or_create_messa_email_local_part(user_id, value.strip())
        pool = await get_pool()
        async with pool.acquire() as conn:
            fresh_row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if fresh_row:
                row = fresh_row

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


async def get_messages_since(user_id: int, since: datetime) -> list[dict[str, Any]]:
    """Every message for this user from `since` onward, oldest first -- the
    daily memory batch job's own input (messa/memory.py's
    run_daily_memory_batch), NOT used on the live per-turn hot path (that's
    get_recent_messages above, unchanged). Separate function rather than
    reusing get_recent_messages with a big limit: this needs a real time
    window (a whole day's worth, whatever that count turns out to be), not
    a fixed row count."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM message_history WHERE user_id = $1 AND timestamp >= $2
            ORDER BY timestamp ASC
            """,
            user_id, since,
        )
        return _rows(rows)


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

def _load_meta(row: dict[str, Any]) -> dict[str, Any]:
    """Every cron_jobs row's `meta` column (migrations/024_task_routines.sql)
    is JSON-serialized TEXT, same convention as pending_actions.payload --
    this is the one place that unpacks it back to a dict. Pre-migration-024
    (no `meta` column at all) or a row somehow stored with an empty/invalid
    value both come back as {} rather than raising -- a routine with no
    extra behavior configured is supposed to look exactly like a routine
    that predates this feature entirely."""
    raw = row.get("meta")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


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


async def reschedule_cron_job(
    cron_id: int, next_run_at: datetime, meta_patch: dict[str, Any] | None = None,
) -> None:
    """Sets the next fire time (used both for a normal recurring reschedule
    and for sub-feature #7 -- a user's plain-language "ask me again in an
    hour" reply resolving to reschedule_routine). `meta_patch`, when given,
    is merged into the existing `meta` (not replaced) in the SAME statement
    -- e.g. bumping last_notified_at/escalation_count together with the new
    next_run_at, so a caller never has to make two round trips or risk a
    torn read between them. No-ops the merge (silently ignores meta_patch)
    pre-migration-024, same "degrade the optional part" pattern as the rest
    of this module."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        has_meta = bool(meta_patch) and await _has_column(conn, "cron_jobs", "meta")
        if has_meta:
            row = await conn.fetchrow(
                "SELECT meta FROM cron_jobs WHERE id = $1", cron_id
            )
            current = _load_meta(dict(row)) if row else {}
            current.update(meta_patch)
            await conn.execute(
                "UPDATE cron_jobs SET last_run_at = NOW(), next_run_at = $2, meta = $3 WHERE id = $1",
                cron_id, next_run_at, json.dumps(current, default=_json_default),
            )
        else:
            await conn.execute(
                "UPDATE cron_jobs SET last_run_at = NOW(), next_run_at = $2 WHERE id = $1",
                cron_id, next_run_at,
            )


async def update_cron_job_meta(cron_id: int, patch: dict[str, Any]) -> None:
    """Merges `patch` into a cron_jobs row's existing meta without touching
    schedule/status -- used for bookkeeping-only updates (recording a failed
    attempt's count, an outcome summary) where reschedule_cron_job's
    combined write doesn't apply. No-op pre-migration-024."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "cron_jobs", "meta"):
            return
        row = await conn.fetchrow("SELECT meta FROM cron_jobs WHERE id = $1", cron_id)
        if row is None:
            return
        current = _load_meta(dict(row))
        current.update(patch)
        await conn.execute(
            "UPDATE cron_jobs SET meta = $2 WHERE id = $1",
            cron_id, json.dumps(current, default=_json_default),
        )


async def set_cron_job_status(
    user_id: int, cron_id: int, status: str, meta_patch: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`meta_patch` (optional): merged into `meta` in the same statement --
    used when a status change also needs to record WHY (e.g. finish_routine
    setting status='cancelled' alongside meta.ended_reason='completed_by_agent'
    and meta.outcome, or the cron loop retiring an expired/exhausted
    autonomous job with meta.ended_reason='expired'/'max_attempts')."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if meta_patch and await _has_column(conn, "cron_jobs", "meta"):
            existing = await conn.fetchrow(
                "SELECT meta FROM cron_jobs WHERE id = $1 AND user_id = $2", cron_id, user_id
            )
            if existing is None:
                return {}
            current = _load_meta(dict(existing))
            current.update(meta_patch)
            row = await conn.fetchrow(
                "UPDATE cron_jobs SET status = $3, meta = $4 WHERE id = $1 AND user_id = $2 RETURNING *",
                cron_id, user_id, status, json.dumps(current, default=_json_default),
            )
        else:
            row = await conn.fetchrow(
                "UPDATE cron_jobs SET status = $3 WHERE id = $1 AND user_id = $2 RETURNING *",
                cron_id, user_id, status,
            )
        return dict(row) if row else {}


async def has_user_replied_since(user_id: int, since: datetime) -> bool:
    """Sub-feature #9 (polite escalation on non-response): has the user sent
    ANY inbound message since `since`? Used right before a follow-up/notify
    fire to decide whether to escalate (shorten pacing, more direct wording)
    or hold steady -- a real reply of any kind resets escalation regardless
    of what it said, since judging whether it actually addressed the
    follow-up is exactly the kind of open-ended judgment call that belongs
    to Messa's own next live turn, not this poller."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT 1 FROM message_history
            WHERE user_id = $1 AND role = 'user' AND timestamp > $2
            LIMIT 1
            """,
            user_id, since,
        )
        return row is not None


# ---------------------------------------------------------------------------
# Digest queue (sub-feature #3, migrations/024_task_routines.sql) -- an
# autonomous or notify routine tagged meta.digest=true writes its result
# here instead of texting immediately; server.py's _production_digest_loop
# decides WHEN to flush a user's queue (their local morning hour, or a
# backstop count) and calls the read/clear functions below to do it. This
# module deliberately does no timing/scheduling judgment itself -- same
# "db.py doesn't know about agents or business timing" boundary as the rest
# of the file.
# ---------------------------------------------------------------------------

async def enqueue_digest_item(user_id: int, cron_job_id: int | None, message_text: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO digest_queue (user_id, cron_job_id, message_text) VALUES ($1, $2, $3)",
            user_id, cron_job_id, message_text,
        )


async def get_users_with_pending_digest_items() -> list[dict[str, Any]]:
    """One row per user with at least one queued item: phone_number/timezone
    (for the flush decision and the send itself), pending_count, and the
    oldest item's created_at -- so a caller can decide per-user whether it's
    that user's local flush hour yet or enough has piled up to flush early,
    without a second query per user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.id AS user_id, u.phone_number, u.timezone,
                   COUNT(dq.id) AS pending_count, MIN(dq.created_at) AS oldest_item_at
            FROM digest_queue dq JOIN users u ON u.id = dq.user_id
            GROUP BY u.id, u.phone_number, u.timezone
            """
        )
        return _rows(rows)


async def get_pending_digest_items(user_id: int) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM digest_queue WHERE user_id = $1 ORDER BY created_at", user_id
        )
        return _rows(rows)


async def clear_digest_items_for_user(user_id: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM digest_queue WHERE user_id = $1", user_id)


# V3-autonomous.md Pillar 1: conflict auto-supersede -- deterministic,
# zero-token recipient-collision detection for routines. Extraction only
# (findall, not a validator): a false-positive-looking "email" here just
# means two routines fail to match each other, the safe direction to be
# wrong in.
_ROUTINE_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)


async def _auto_supersede_conflicting_routine(
    conn: asyncpg.Connection, user_id: int, new_job_id: int, prompt_or_task: str,
) -> dict[str, Any] | None:
    """If the brand-new routine `new_job_id` targets the same recipient
    email address as exactly one other still-active/paused routine for
    this user, cancel that older one and return its (now-cancelled) row so
    the caller can mention it in its reply -- the field incident this
    fixes: a schedule created for a recipient (e.g. kj@mangustacap.com)
    twice, once via Gmail and again over SMS, left BOTH live instead of
    the second replacing the first.

    Same "match on exactly ONE candidate only" safety rule as
    _retire_legacy_briefing_if_any above: zero matches (no email in this
    routine, or no other routine shares one) or MULTIPLE matches (more
    than one existing routine mentions the same address -- genuinely
    ambiguous which one this is meant to replace, if any) both mean don't
    touch anything. Guessing wrong here means silently cancelling a real
    user's real automation, which is a far worse failure than occasionally
    leaving a genuine duplicate for the user to clean up themselves."""
    emails = set(_ROUTINE_EMAIL_RE.findall(prompt_or_task or ""))
    if not emails:
        return None
    candidates = await conn.fetch(
        "SELECT id, prompt_or_task FROM cron_jobs WHERE user_id = $1 AND id != $2 "
        "AND status IN ('active', 'paused')",
        user_id, new_job_id,
    )
    matches = [
        c for c in candidates
        if emails & set(_ROUTINE_EMAIL_RE.findall(c["prompt_or_task"] or ""))
    ]
    if len(matches) != 1:
        return None
    row = await conn.fetchrow(
        "UPDATE cron_jobs SET status = 'cancelled' WHERE id = $1 RETURNING *", matches[0]["id"],
    )
    return dict(row) if row else None


async def _insert_cron_job(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Applier for the 'create_routine' gated action (routines_tools.py's
    propose_create_routine) -- confirm_pending_action hands this a payload
    that's been through one full json.dumps/json.loads round trip (see
    that function's own comment on `payload` columns being TEXT, not
    JSONB), so `next_run_at` arrives as an ISO-8601-ish STRING here, not a
    real datetime object, exactly like every other gated action's payload
    in this file (see _insert_calendar_event's use of _parse_dt just above)
    -- despite what an earlier version of this function's docstring
    (incorrectly) claimed. Run through _parse_dt like everything else that
    touches a timestamptz column, or asyncpg raises a DataError.

    execution_mode/meta are written only if migration 024 has run
    (_has_column-guarded) -- pre-migration, this behaves exactly as it did
    before this feature existed: a plain recurring job, no meta.

    Conflict auto-supersede (see _auto_supersede_conflicting_routine just
    above) runs AFTER the insert, on this same connection/transaction
    (confirm_pending_action wraps every applier call in one), and is
    gated behind config.CONFLICT_AUTO_SUPERSEDE_ENABLED -- when it fires,
    the cancelled row is stashed under the "_superseded_job" key so
    registry.py's confirm_pending_action tool can mention it; when it
    doesn't, that key is simply absent (never set to None), so a plain
    `"_superseded_job" in result` check is exactly as good as a truthy
    check and neither breaks any existing caller that doesn't know this
    key exists at all."""
    user_tz = payload.get("user_timezone", config.DEFAULT_TIMEZONE)
    next_run_at = _parse_dt(payload["next_run_at"], user_tz)
    has_mode = await _has_column(conn, "cron_jobs", "execution_mode")
    has_meta = await _has_column(conn, "cron_jobs", "meta")
    if has_mode and has_meta:
        row = await conn.fetchrow(
            """
            INSERT INTO cron_jobs
                (user_id, prompt_or_task, cron_expression, user_timezone, next_run_at,
                 status, execution_mode, meta)
            VALUES ($1, $2, $3, $4, $5, 'active'::cron_job_status, $6, $7) RETURNING *
            """,
            user_id, payload["prompt_or_task"], payload["cron_expression"], user_tz, next_run_at,
            payload.get("execution_mode", "autonomous"),
            json.dumps(payload.get("meta") or {}, default=_json_default),
        )
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO cron_jobs (user_id, prompt_or_task, cron_expression, user_timezone, next_run_at, status)
            VALUES ($1, $2, $3, $4, $5, 'active'::cron_job_status) RETURNING *
            """,
            user_id, payload["prompt_or_task"], payload["cron_expression"], user_tz, next_run_at,
        )
    result = dict(row)
    if config.CONFLICT_AUTO_SUPERSEDE_ENABLED:
        superseded = await _auto_supersede_conflicting_routine(
            conn, user_id, row["id"], payload["prompt_or_task"],
        )
        if superseded:
            result["_superseded_job"] = superseded
    return result


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
    via propose_create_routine); a job this function itself tagged
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
    than going through propose_action/create_routine's confirmation
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


async def _insert_broadcast(conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """Applier for the 'broadcast_message' gated action (admin_tools.py's
    propose_broadcast_message) -- only ever stages a `broadcasts` row with
    status='pending'. The actual fan-out send happens asynchronously in
    server.py's _production_broadcast_loop, not here: sending to a whole
    user base can take a while and shouldn't block the admin's confirming
    chat turn (see that loop's own docstring)."""
    row = await conn.fetchrow(
        "INSERT INTO broadcasts (created_by, message_text, status) VALUES ($1, $2, 'pending') RETURNING *",
        user_id, payload["message_text"],
    )
    return dict(row)


async def _insert_call_session_confirmed(
    conn: asyncpg.Connection, user_id: int, payload: dict[str, Any]
) -> dict[str, Any]:
    """Applier for the 'place_call' gated action (messa/tools/call_tools.py's
    propose_call) -- only ever stages a call_sessions row with
    status='confirmed'. The actual dial (a real, slow, failable HTTP call
    to messa/channels/vapi.py's create_call) happens as a SEPARATE step
    immediately after, outside this transaction, triggered by registry.py's
    confirm_pending_action tool -- same reasoning _insert_broadcast's own
    docstring gives for why broadcasts' fan-out send isn't done in here
    either: a slow/failable external call has no business holding this
    transaction open, or risking "call placed, DB rolled back, no record
    of it ever existing."

    call_id (our own app-generated correlation id -- distinct from Vapi's
    own provider_call_id, which doesn't exist until AFTER the dial
    succeeds) is generated HERE, at confirm time, not earlier when
    propose_call first built the payload -- nothing needs it before this
    row exists.

    pending_action_id is deliberately left unset (NULL) -- this applier's
    signature, like every other one in _APPLIERS, only ever receives
    (conn, user_id, payload), not the pending_action's own id, matching
    _insert_broadcast's identical omission just above."""
    call_id = str(uuid.uuid4())
    row = await conn.fetchrow(
        """
        INSERT INTO call_sessions
            (call_id, user_id, provider, destination_number, business_name,
             task_description, scratchpad_snapshot, allowed_info_fields,
             status, max_duration_seconds)
        VALUES ($1, $2, 'vapi', $3, $4, $5, $6, $7, 'confirmed', $8)
        RETURNING *
        """,
        call_id, user_id, payload["destination_number"], payload.get("business_name"),
        payload["task_description"], payload.get("scratchpad_snapshot", "{}"),
        payload.get("allowed_info_fields", "[]"), payload["max_duration_seconds"],
    )
    return dict(row)


async def get_pending_broadcasts() -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM broadcasts WHERE status = 'pending' ORDER BY created_at")
        return _rows(rows)


async def claim_broadcast(broadcast_id: int) -> bool:
    """Atomic claim (status='pending' -> 'sending' in one statement) so two
    overlapping poll cycles -- or, later, two server instances -- can never
    both fan out the same broadcast twice."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "UPDATE broadcasts SET status = 'sending' WHERE id = $1 AND status = 'pending' RETURNING id",
            broadcast_id,
        )
        return row is not None


async def complete_broadcast(broadcast_id: int, total: int, sent: int, failed: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE broadcasts SET status = 'completed', total_recipients = $2,
                sent_count = $3, failed_count = $4, completed_at = NOW()
            WHERE id = $1
            """,
            broadcast_id, total, sent, failed,
        )


async def list_broadcasts(limit: int = 10) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM broadcasts ORDER BY created_at DESC LIMIT $1", limit)
        return _rows(rows)


# ---------------------------------------------------------------------------
# Pending actions -- the confirm-before-write gate.
#
# Per explicit product decision, only SCHEDULING requires confirmation:
# create/update/delete calendar event, and creating a new recurring/one-time
# automation (routines_agent's routines -- also a scheduling action). Tasks,
# reminders, notes, and contacts all write directly (see their sections
# above) since they're low-stakes and trivially reversible with a follow-up
# message -- an earlier version of this gate also covered those, which
# added confirmation friction the product decision explicitly removed.
#
# One deliberate second category joined it later: broadcast_message. It's
# not scheduling, but it's the same shape of risk that motivated the
# original rule in the first place (something that, once it fires, reaches
# a lot of people at once and can't be walked back) -- admin_tools.py's own
# "preview -> approval -> execute" requirement is exactly this gate, reused
# rather than building a second confirm mechanism.
#
# A third category (plans/glowing-forging-pumpkin.md): place_call. A real
# outbound phone call is the highest-stakes single action this system can
# take on a user's behalf -- money can be spent, a real stranger picks up
# and starts talking to something acting for the user -- so it gets the
# exact same "confirm before it's real" treatment as scheduling and
# broadcasts, reusing this mechanism rather than inventing a new one (the
# synchronous ApprovalGate/trace_tool(destructive=True) path is hard-
# blocked in production via DenyApprovalGate anyway, and was never meant
# for this -- see call_tools.py's own notes on why this gate was chosen).
# ---------------------------------------------------------------------------

_APPLIERS = {
    "create_calendar_event": _insert_calendar_event,
    "update_calendar_event": _update_calendar_event,
    "delete_calendar_event": _delete_calendar_event,
    "create_routine": _insert_cron_job,
    "broadcast_message": _insert_broadcast,
    "place_call": _insert_call_session_confirmed,
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


async def log_audit_event(user_id: int, action_type: str, payload: dict[str, Any]) -> None:
    """Standalone audit_logs write for an event that happens OUTSIDE any
    pending_actions confirm flow -- confirm_pending_action above writes its
    own audit_logs row inline (inside that same transaction, since it's
    already there); this is for the one caller today that has no
    pending_actions row to hang off of at all: messa/tools/call_tools.py's
    mid-call info-tool dispatch, triggered by a webhook long after the
    original 'place_call' confirmation already happened and was logged
    separately. source_pending_action_id is left NULL here on purpose --
    there's no single pending action this specific event confirms.
    Best-effort: swallows its own failures (logged, not raised) so a
    logging hiccup can never be the reason a real mid-call response fails
    to go out."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO audit_logs (user_id, action_type, payload) VALUES ($1, $2, $3)",
                user_id, action_type, json.dumps(payload, default=_json_default),
            )
    except Exception as e:  # noqa: BLE001 - audit logging must never break the caller
        console.system(f"log_audit_event: failed to write audit log ({action_type}): {e}")


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


async def cancel_all_active_deepsearch_sessions(user_id: int) -> int:
    """Marks all 'active' deepsearch sessions for this user as 'abandoned'
    (the enum value representing cancelled/halted sessions) and clears any
    pending OTP expectations for this user. Returns the count of cancelled sessions."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = 0
        if await _has_table(conn, "deepsearch_sessions"):
            res = await conn.execute(
                "UPDATE deepsearch_sessions SET status = 'abandoned'::deepsearch_status, "
                "summary = '(cancelled by user)' WHERE user_id = $1 AND status = 'active'::deepsearch_status",
                user_id,
            )
            try:
                count = int(res.split(" ")[-1])
            except Exception:
                count = 0
        if await _has_table(conn, "deepsearch_otp_expectations"):
            await conn.execute(
                "DELETE FROM deepsearch_otp_expectations WHERE user_id = $1",
                user_id,
            )
        return count


async def get_latest_deepsearch_session(user_id: int) -> dict[str, Any] | None:
    """Returns the most recent deepsearch session row for this user, or None."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_sessions"):
            return None
        row = await conn.fetchrow(
            """
            SELECT id, title, status, steps_used, created_at, updated_at, summary, live_view_url
            FROM deepsearch_sessions
            WHERE user_id = $1
            ORDER BY id DESC
            LIMIT 1
            """,
            user_id,
        )
        return dict(row) if row else None


async def list_active_otp_expectations(user_id: int) -> list[dict[str, Any]]:
    """Lists any OTP expectation rows actively pending for this user."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_otp_expectations"):
            return []
        rows = await conn.fetch(
            """
            SELECT id, sender_filter, created_at
            FROM deepsearch_otp_expectations
            WHERE user_id = $1 AND status = 'pending'
            ORDER BY created_at DESC
            """,
            user_id,
        )
        return _rows(rows)


async def get_recent_assistant_messages(user_id: int, limit: int = 3) -> list[dict[str, Any]]:
    """Returns the user's most recent outgoing assistant messages with timestamps."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "message_history"):
            return []
        rows = await conn.fetch(
            """
            SELECT id, role, content, timestamp, channel
            FROM message_history
            WHERE user_id = $1 AND role = 'assistant'
            ORDER BY timestamp DESC
            LIMIT $2
            """,
            user_id,
            limit,
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
# Email verification-code relay (migrations/028_deepsearch_otp_expectations.sql).
# Additive/no-op-safe, same _has_table pattern as deepsearch_human_help_requests
# above -- these all silently no-op until migration 028 has been applied.
#
# create_otp_expectation/get_otp_expectation/expire_otp_expectation are
# called from inside the in-flight deepsearch tool call itself (see
# tools/deepsearch_tools.py's await_email_verification_code); resolve_otp_
# expectation_from_email is called from server.py's personal-email webhook,
# a completely separate request -- the same "two different call sites"
# split human_help already uses, and for the same reason: whichever inbound
# email lands doesn't depend on the original tool call's async task still
# being alive to see it arrive.
# ---------------------------------------------------------------------------

# Prefers a code that appears near an actual verification-code keyword
# (catches "Your code is 482913" even when the email ALSO contains other
# numbers -- an order number, a year, a support phone number) before
# falling back to the first bare 4-8 digit run in the message. Deliberately
# simple (no per-sender templates) -- OTP emails are short and this two-
# tier heuristic has covered every real one seen in testing; a code that
# slips past both patterns just means this returns None and the normal
# "you got an email" notification flow runs instead, never a silent drop.
_OTP_CODE_NEAR_KEYWORD_RE = re.compile(
    r"(?:verification code|confirmation code|security code|one[- ]time (?:code|password|pass)|"
    r"\bOTP\b|\bPIN\b|\bcode\b|\bpasscode\b)\D{0,20}(\d{4,8})",
    re.IGNORECASE,
)
_OTP_CODE_STANDALONE_RE = re.compile(r"\b(\d{4,8})\b")


def _extract_otp_code(subject: str | None, body_text: str | None) -> str | None:
    text = f"{subject or ''}\n{body_text or ''}"
    m = _OTP_CODE_NEAR_KEYWORD_RE.search(text)
    if m:
        return m.group(1)
    m = _OTP_CODE_STANDALONE_RE.search(text)
    return m.group(1) if m else None


async def create_otp_expectation(
    user_id: int, deepsearch_session_id: int | None, tab_marker: str, sender_filter: str | None,
) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_otp_expectations"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO deepsearch_otp_expectations
                (user_id, deepsearch_session_id, tab_marker, sender_filter)
            VALUES ($1, $2, $3, $4) RETURNING *
            """,
            user_id, deepsearch_session_id, tab_marker, (sender_filter or "").strip() or None,
        )
        return dict(row) if row else None


async def get_otp_expectation(expectation_id: int) -> dict[str, Any] | None:
    """Polled every config.DEEPSEARCH_OTP_WAIT_POLL_INTERVAL_SECONDS by the
    waiting tool call -- see that function's own docstring for why this is
    a DB poll rather than an in-process signal (the resolving webhook call
    and the waiting tool call are two separate requests, possibly with no
    live reference to each other at all)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_otp_expectations"):
            return None
        row = await conn.fetchrow("SELECT * FROM deepsearch_otp_expectations WHERE id = $1", expectation_id)
        return dict(row) if row else None


async def expire_otp_expectation(expectation_id: int) -> None:
    """Called by the waiting tool call itself once it gives up -- marks the
    row so a LATER, unrelated email (a different site's code, or the same
    site's retry) can never be mistaken for an answer to a wait that has
    already ended. WHERE status = 'pending' guards the harmless race where
    the row resolved in the instant between the tool's last poll and this
    call -- never downgrade an already-resolved row back to expired."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_otp_expectations"):
            return
        await conn.execute(
            "UPDATE deepsearch_otp_expectations SET status = 'expired', resolved_at = NOW() "
            "WHERE id = $1 AND status = 'pending'",
            expectation_id,
        )


async def resolve_otp_expectation_from_email(
    user_id: int, from_address: str, subject: str | None, body_text: str | None,
) -> dict[str, Any] | None:
    """Called from server.py's personal-email webhook for every inbound
    email, before it decides whether to start a normal notification turn.
    Returns the newly-resolved row (now carrying the extracted code) if
    this email answered a pending expectation for this user, else None --
    a None return means "nothing was waiting, or nothing in this email
    looked like a code," and the webhook's normal flow should proceed
    exactly as if this function didn't exist.

    Matching: among this user's still-'pending' rows (oldest first), prefer
    one whose sender_filter appears in the from-address or subject; if none
    match by filter, fall back to the oldest row that has NO filter at all
    (a model that couldn't guess a keyword up front). A row with a filter
    that matches nothing here is left pending -- it's still waiting for a
    DIFFERENT email, not resolved by this one."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "deepsearch_otp_expectations"):
            return None
        pending = await conn.fetch(
            "SELECT * FROM deepsearch_otp_expectations WHERE user_id = $1 AND status = 'pending' "
            "ORDER BY created_at ASC",
            user_id,
        )
        if not pending:
            return None
        code = _extract_otp_code(subject, body_text)
        if not code:
            return None
        haystack = f"{from_address}\n{subject or ''}".lower()
        chosen = None
        for row in pending:
            sf = (row["sender_filter"] or "").strip().lower()
            if sf and sf in haystack:
                chosen = row
                break
        if chosen is None:
            for row in pending:
                if not (row["sender_filter"] or "").strip():
                    chosen = row
                    break
        if chosen is None:
            return None
        updated = await conn.fetchrow(
            "UPDATE deepsearch_otp_expectations SET status = 'resolved', code = $2, resolved_at = NOW() "
            "WHERE id = $1 RETURNING *",
            chosen["id"], code,
        )
        return dict(updated) if updated else None


async def find_recent_otp_in_inbox(
    user_id: int, sender_filter: str | None = None, max_age_seconds: int = 180,
) -> dict[str, Any] | None:
    """Checks messa_email_messages for any inbound email that arrived within the
    last max_age_seconds, optionally matching sender_filter in from_address/subject,
    and containing an extractable OTP code. Used to catch the race where a website
    emailed the code immediately before deepsearch called await_email_verification_code.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return None
        rows = await conn.fetch(
            """
            SELECT from_address, subject, body_text, created_at
            FROM messa_email_messages
            WHERE user_id = $1
              AND direction = 'inbound'
              AND created_at >= NOW() - ($2 || ' seconds')::interval
            ORDER BY created_at DESC
            LIMIT 10
            """,
            user_id, str(int(max_age_seconds)),
        )
        if not rows:
            return None

        sf = (sender_filter or "").strip().lower()
        for r in rows:
            haystack = f"{r['from_address'] or ''}\n{r['subject'] or ''}".lower()
            if sf and sf not in haystack:
                continue
            code = _extract_otp_code(r["subject"], r["body_text"])
            if code:
                return {
                    "code": code,
                    "from_address": r["from_address"],
                    "subject": r["subject"],
                    "created_at": r["created_at"],
                }

        if not sf:
            for r in rows:
                code = _extract_otp_code(r["subject"], r["body_text"])
                if code:
                    return {
                        "code": code,
                        "from_address": r["from_address"],
                        "subject": r["subject"],
                        "created_at": r["created_at"],
                    }
        return None


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


async def mark_apps_onboarding_asked(user_id: int) -> None:
    """Records that Messa has asked (once, deterministically, see cli.py's
    _onboarding_complete_messages) what apps this user uses day to day --
    set the moment the question is ASKED, regardless of how (or whether)
    the user answers, so it's never repeated. migrations/032_app_connect_
    queue.sql; graceful no-op pre-migration, same _has_column pattern as
    everywhere else in this file."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "apps_onboarding_asked"):
            return
        await conn.execute("UPDATE users SET apps_onboarding_asked = TRUE WHERE id = $1", user_id)


async def set_pending_app_connect_queue(user_id: int, toolkit_slugs: list[str]) -> None:
    """Overwrites this user's queued-apps-to-offer list (see migrations/032's
    header for the full "ask once, connect one at a time" design) --
    tools/integration_tools.py's queue_app_connections calls this with
    every app AFTER the first one (the first is connected/linked
    immediately, not queued). TEXT column, hand JSON-encoded, matching
    this project's own convention (see this module's docstring) rather
    than JSONB. Graceful no-op pre-migration."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "pending_app_connect_queue"):
            return
        await conn.execute(
            "UPDATE users SET pending_app_connect_queue = $2 WHERE id = $1",
            user_id, json.dumps(toolkit_slugs),
        )


async def pop_next_pending_app_connect(user_id: int) -> str | None:
    """Atomically pops and returns the FRONT toolkit slug of this user's
    queued-apps list, or None if nothing's queued (or the migration hasn't
    run). Called by server.py's _production_app_connection_poll_loop right
    after it notices the user's CURRENT app connection go ACTIVE -- see
    migrations/032's header for the full one-at-a-time design. A plain
    SELECT-then-UPDATE rather than a single atomic SQL expression: this is
    read/written by exactly one background poll loop per process, never
    concurrently for the same user (a user only ever has one connection
    request in flight at a time), so there's no real race to guard against
    here the way there would be for, say, deepsearch_otp_expectations."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "pending_app_connect_queue"):
            return None
        row = await conn.fetchrow(
            "SELECT pending_app_connect_queue FROM users WHERE id = $1", user_id,
        )
        raw = row["pending_app_connect_queue"] if row else None
        if not raw:
            return None
        try:
            queue = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not queue:
            return None
        next_slug, rest = queue[0], queue[1:]
        await conn.execute(
            "UPDATE users SET pending_app_connect_queue = $2 WHERE id = $1",
            user_id, json.dumps(rest),
        )
        return next_slug


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
# Memory (migrations/027_memory.sql) -- the cheap profile-digest column the
# live per-turn hot path reads (see config.UserContext.memory_profile /
# cli._context_from_row). The ONLY writer is messa/memory.py's daily batch
# job, which derives the text from Mem0 (the actual episodic/semantic
# store, RLS-scoped, never touched on the hot path -- see messa/memory.py's
# own docstring). Same _has_column-guarded, additive-safe shape as
# set_default_email_provider just above.
# ---------------------------------------------------------------------------

async def claim_daily_memory_batch_run(run_date) -> bool:
    """True if THIS call is the one that gets to run today's memory batch
    (messa/memory.py's run_daily_memory_batch) -- False if another call
    already claimed run_date (a restart shortly after a completed run, or,
    in principle, a second running instance). INSERT ... ON CONFLICT DO
    NOTHING is the whole mechanism: no separate lock table, no explicit
    row locking -- Postgres's own uniqueness constraint on run_date is
    what prevents a double-run. Returns False (never raises) if migration
    027 hasn't been applied yet, same as every other _has_column-guarded
    function in this file -- the batch loop just skips a day rather than
    crashing."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        has_table = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'memory_batch_runs')"
        )
        if not has_table:
            return False
        result = await conn.execute(
            "INSERT INTO memory_batch_runs (run_date) VALUES ($1) ON CONFLICT DO NOTHING", run_date,
        )
        return result == "INSERT 0 1"


async def complete_daily_memory_batch_run(run_date, users_processed: int, users_failed: int) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE memory_batch_runs
            SET completed_at = NOW(), users_processed = $2, users_failed = $3
            WHERE run_date = $1
            """,
            run_date, users_processed, users_failed,
        )


async def set_memory_profile(user_id: int, profile_text: str) -> dict[str, Any] | None:
    """Overwrites the whole digest (not an append/merge -- the daily batch
    job always regenerates the full digest from Mem0's current state, so
    there's nothing to merge). Returns the updated row, or None if
    migration 027 hasn't been applied yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "memory_profile"):
            return None
        row = await conn.fetchrow(
            "UPDATE users SET memory_profile = $2, memory_profile_updated_at = NOW() WHERE id = $1 RETURNING *",
            user_id, profile_text,
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

def _slugify_local_part(name: str | None, user_id: int) -> str | None:
    """Best-effort local-part from a display name ("Jane Doe" -> "janedoe").
    Returns None if no name is available yet, or if the name contains no
    alphanumeric characters once stripped. We wait until we have the user's
    name before assigning a Messa email address (<username>@textmessa.com).
    Deliberately plain alphanumeric-only: an RFC 5322 local-part technically
    allows a lot more, but plenty of real mail providers choke on anything
    fancier, and this is meant to be easy to read aloud/type into a form,
    not maximally expressive."""
    if not name:
        return None
    base = re.sub(r"[^a-z0-9]+", "", name.lower())[:24]
    return base if base else None


async def get_or_create_messa_email_local_part(user_id: int, name: str | None) -> str | None:
    """Idempotent: returns the existing local part if this user already has
    one, otherwise claims one and persists it once the user's name is known.
    Called on every cli.load_user_context (same "cheap after the first time"
    shape as get_or_create_live_share_token), but waits until the user has
    provided their name before provisioning an address (<username>@textmessa.com).

    Collision handling: tries the clean slug first (e.g. "vinay"), and
    falls back to a decorated one ("vinay10", using this user's own id)
    on an actual collision with an existing user.

    Legacy upgrade: if a user previously received a placeholder like 'user10'
    before their name was collected, providing their name upgrades their address
    to <username>@textmessa.com."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_column(conn, "users", "messa_email_local_part"):
            return None
        row = await conn.fetchrow("SELECT messa_email_local_part, name FROM users WHERE id = $1", user_id)
        if row is None:
            return None  # unknown user id -- nothing to provision

        current_part = row["messa_email_local_part"]
        is_placeholder = bool(current_part and re.match(r"^user\d+$", current_part))

        # If user already has a personalized local-part (not a legacy placeholder), keep it
        if current_part and not is_placeholder:
            return current_part

        # Check effective name from argument or DB row
        effective_name = (name or row.get("name") or "").strip()
        base = _slugify_local_part(effective_name, user_id)
        if not base:
            # We don't have the user's name yet -- wait until we get it.
            # Do NOT create or assign a placeholder 'user<id>'.
            return current_part if not is_placeholder else None

        # Clean slug first, then slug + user_id on collision
        candidates = [base, f"{base}{user_id}"]
        for candidate in candidates:
            try:
                await conn.execute(
                    "UPDATE users SET messa_email_local_part = $2 WHERE id = $1", user_id, candidate,
                )
                return candidate
            except asyncpg.UniqueViolationError:
                continue

        # In the exceedingly rare case both base and base+user_id collide
        counter = 1
        while counter < 100:
            candidate = f"{base}{user_id}{counter}"[:64]
            try:
                await conn.execute(
                    "UPDATE users SET messa_email_local_part = $2 WHERE id = $1", user_id, candidate,
                )
                return candidate
            except asyncpg.UniqueViolationError:
                counter += 1
                continue

        return None


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
    auto_submitted: bool = False,
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

    `auto_submitted` (migrations/030_email_loop_safety.sql): True when the
    worker forwarded an RFC 3834 "Auto-Submitted" header on this inbound
    message (an auto-*-value, not a real human hitting send) -- see
    server.py's webhook route for where this is computed. tools/
    personal_inbox_tools.py's reply_to_email refuses an autonomous=True
    reply to a message flagged this way, part of this project's loop-
    safety backstop against two auto-replying mailboxes bouncing forever.
    False (unchanged default) for an ordinary human-sent email.

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
        has_auto_submitted_col = await _has_column(conn, "messa_email_messages", "auto_submitted")
        if has_attachment_col and has_auto_submitted_col:
            row = await conn.fetchrow(
                """
                INSERT INTO messa_email_messages
                    (user_id, direction, thread_id, message_id, in_reply_to, references_header,
                     from_address, to_address, subject, body_text, raw_json, attachment_filename,
                     auto_submitted)
                VALUES ($1, 'inbound', $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (message_id) DO NOTHING
                RETURNING *
                """,
                user_id, thread_id, real_message_id, in_reply_to, references,
                from_address, to_address, subject, body_text, raw_json, attachment_filename,
                auto_submitted,
            )
        elif has_attachment_col:
            # migration 030 not applied yet -- degrade gracefully rather
            # than erroring: the email itself is still worth logging even
            # if we can't note whether it was auto-submitted.
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
            # if we can't note that it had an attachment (or, in turn,
            # whether it was auto-submitted -- that column check is moot
            # if we're already this far back).
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


async def get_recent_outbound_messages_in_thread(
    user_id: int, thread_id: str, limit: int = 2
) -> list[dict[str, Any]]:
    """The most recent `limit` OUTBOUND rows in this thread, newest first --
    backs tools/personal_inbox_tools.py's reply_to_email consecutive-
    autonomous-reply cap (loop-safety backstop against two auto-replying
    mailboxes -- most concerning, two different users' own Messa mailboxes
    -- bouncing an autonomous reply back and forth forever): if the last
    `limit` outbound sends in a thread were ALL sent_autonomously, a
    further autonomous send is refused until a non-autonomous (user-
    directed) reply resets the count. Empty list if this thread has no
    outbound messages yet, or migration 014 hasn't been applied."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "messa_email_messages"):
            return []
        rows = await conn.fetch(
            """
            SELECT * FROM messa_email_messages
            WHERE user_id = $1 AND thread_id = $2 AND direction = 'outbound'
            ORDER BY created_at DESC LIMIT $3
            """,
            user_id, thread_id, limit,
        )
        return [dict(r) for r in rows]


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
# Generated-document shares (migrations/018_generated_document_shares.sql,
# migrations/034_document_share_bytes.sql):
# a public, unguessable-token URL for a file Messa generated, so it can be
# attached to an outbound TEXT message via Sendblue's media_url (which
# needs a URL it can fetch, not raw bytes -- see agents/registry.py's
# send_pdf_over_text, tools/stagehand_tools.py's send_screenshot, and
# server.py's GET /files/{token}).
#
# Migration 034 added file_bytes/media_type so serving never depends on
# the container's local disk (see that migration's own header for the
# full production incident this fixes -- Hugging Face's ephemeral,
# per-replica filesystem). This module is the ONLY place that decides
# whether a share is bytes-backed: create_document_share opportunistically
# reads file_path into file_bytes right here (this call always runs on
# the SAME container/process that just generated the file, before any
# cross-replica risk exists), so existing path-only callers like
# send_pdf_over_text get bytes-backed storage with no call-site changes.
# A caller that already has the bytes in memory (send_screenshot never
# writes to disk at all) can pass file_bytes directly instead.
# ---------------------------------------------------------------------------

async def create_document_share(
    user_id: int,
    file_path: str,
    filename: str,
    *,
    file_bytes: bytes | None = None,
    media_type: str | None = None,
) -> str | None:
    """Mints a fresh unguessable token for `file_path` and records it.
    Same token-generation approach as get_or_create_live_share_token
    (secrets.token_urlsafe) -- not sequential/enumerable. Returns None
    (no-op) if migration 018 hasn't been applied yet, same pattern as
    every other optional-table function in this module.

    file_bytes: pass this when the caller already has the content in
    memory (tools/stagehand_tools.py's send_screenshot -- it never writes
    the PNG to disk at all, so there is no file_path to read back). When
    omitted, this function tries to read `file_path` itself, right here,
    on the assumption that whatever process just called this is the SAME
    one that just generated the file (true for every current caller) --
    a read failure (missing file, permission error) or an oversized file
    is swallowed, not raised, and the share still gets created path-only,
    degrading to the pre-migration-034 disk-serving behavior rather than
    failing the whole operation over what is, for those files, a nice-to-
    have upgrade.

    media_type: stored explicitly when the caller knows it (send_screenshot
    always passes "image/png"). Guessed from `filename`'s extension when
    omitted, same fallback server.py's route already used before this
    migration."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "generated_document_shares"):
            return None
        token = secrets.token_urlsafe(24)
        has_bytes_col = await _has_column(conn, "generated_document_shares", "file_bytes")

        resolved_bytes = file_bytes
        if resolved_bytes is None and has_bytes_col:
            # Sanity cap on how large a file gets inlined into a Postgres
            # row -- reuses the existing outbound-MMS attachment cap (the
            # only realistic consumer of these bytes is Sendblue's
            # media_url fetch, which has the same practical ceiling)
            # rather than inventing a separate number. An oversized file
            # just falls back to file_path/disk-serving below instead of
            # failing the whole share.
            try:
                data = Path(file_path).read_bytes()
                if len(data) <= config.MAX_SMS_ATTACHMENT_BYTES:
                    resolved_bytes = data
            except OSError:
                resolved_bytes = None

        if not media_type:
            media_type = mimetypes.guess_type(filename)[0]

        if has_bytes_col:
            await conn.execute(
                "INSERT INTO generated_document_shares "
                "(user_id, token, file_path, filename, file_bytes, media_type) "
                "VALUES ($1, $2, $3, $4, $5, $6)",
                user_id, token, file_path, filename, resolved_bytes, media_type,
            )
        else:
            # Pre-migration-034 deployment -- degrade to the original
            # path-only row rather than erroring.
            await conn.execute(
                "INSERT INTO generated_document_shares (user_id, token, file_path, filename) "
                "VALUES ($1, $2, $3, $4)",
                user_id, token, file_path, filename,
            )
        return token


async def get_document_share_by_token(token: str) -> dict[str, Any] | None:
    """Looked up by server.py's public GET /files/{token} route, which
    prefers the row's file_bytes (migrations/034) when present -- serving
    straight from Postgres with zero dependency on this container's local
    disk -- and only falls back to re-validating file_path against
    config.OUTPUTS_DIR (config.resolve_output_file) for a row with no
    bytes stored. This function is just the lookup either way."""
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


# ---------------------------------------------------------------------------
# Call sessions -- outbound voice-calling feature (migrations/
# 035_call_sessions.sql, messa/plans.py's call_minutes, messa/channels/
# vapi.py). One row per outbound call Messa places on a user's behalf.
# Plain CRUD, same _has_table graceful-degrade shape as active_tasks/
# deepsearch_sessions above: a process running before migration 035 has
# landed just gets no call history (every function below returns
# None/[]/False rather than crashing).
#
# NOTE for a future reader: the 'place_call' pending-actions applier (the
# function that actually INSERTs the very first row for a call, at
# confirm-before-dial time) lives with the rest of _APPLIERS further up
# this file, not here -- this section is read/update-focused CRUD for a
# row that already exists.
# ---------------------------------------------------------------------------

async def create_call_session(
    user_id: int,
    *,
    call_id: str,
    destination_number: str,
    task_description: str,
    max_duration_seconds: int,
    pending_action_id: int | None = None,
    business_name: str | None = None,
    scratchpad_snapshot: str = "{}",
    allowed_info_fields: str = "[]",
    provider: str = "vapi",
    status: str = "confirmed",
) -> dict[str, Any] | None:
    """Creates the first row for a call. Called by the 'place_call' applier
    (confirm-before-dial time, status stays 'confirmed' -- no external HTTP
    call happens inside that transaction, see _APPLIERS' own docstring
    above) -- kept here rather than inlined there so every other caller
    that ever needs to create a call_sessions row (a future admin tool,
    a test fixture) gets the same defaults and the same _has_table guard
    without duplicating the INSERT."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "call_sessions"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO call_sessions
                (call_id, user_id, pending_action_id, provider, destination_number,
                 business_name, task_description, scratchpad_snapshot, allowed_info_fields,
                 status, max_duration_seconds)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            RETURNING *
            """,
            call_id, user_id, pending_action_id, provider, destination_number,
            business_name, task_description, scratchpad_snapshot, allowed_info_fields,
            status, max_duration_seconds,
        )
        return dict(row) if row else None


async def get_call_session(call_id: str) -> dict[str, Any] | None:
    """Looked up by our own app-generated call_id -- e.g. right after
    dial_confirmed_call creates the row, before Vapi's create-call response
    (and therefore provider_call_id) exists yet."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "call_sessions"):
            return None
        row = await conn.fetchrow("SELECT * FROM call_sessions WHERE call_id = $1", call_id)
        return dict(row) if row else None


async def get_call_session_by_provider_id(provider_call_id: str) -> dict[str, Any] | None:
    """The webhook-side lookup: every inbound Vapi webhook event (mid-call
    tool-call, end-of-call report) carries Vapi's OWN call id, never our
    call_id. Returns None for an id that isn't in our table at all --
    callers MUST treat that as a hard rejection (a forged/replayed webhook,
    or an event for a call this deployment never placed), not as "not
    found yet, retry" -- see call_tools.py's webhook-handling security
    notes."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "call_sessions"):
            return None
        row = await conn.fetchrow(
            "SELECT * FROM call_sessions WHERE provider_call_id = $1", provider_call_id,
        )
        return dict(row) if row else None


async def get_active_call_session_for_user(user_id: int) -> dict[str, Any] | None:
    """The row (if any) currently occupying this user's concurrency slot --
    used by call_control's active_count/is_active. "Active" here means
    anywhere between confirmed-but-not-yet-dialed and actually in progress
    -- everything except the two terminal states."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "call_sessions"):
            return None
        row = await conn.fetchrow(
            """
            SELECT * FROM call_sessions
            WHERE user_id = $1 AND status NOT IN ('ended', 'failed')
            ORDER BY created_at DESC LIMIT 1
            """,
            user_id,
        )
        return dict(row) if row else None


async def update_call_session(call_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Generic partial-update by our own call_id -- every state transition
    (dialing -> ringing -> in_progress -> ended/failed, plus
    provider_call_id arriving, plus the end-of-call metering fields) goes
    through this one function rather than a bespoke UPDATE per transition,
    so there's exactly one place that stamps updated_at and one place a
    future caller needs to check for the allowed column list. `fields`
    keys must be real column names -- this is an internal helper, not
    exposed to any model-facing tool, so that trust boundary is fine."""
    if not fields:
        return await get_call_session(call_id)
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "call_sessions"):
            return None
        set_clauses = []
        values: list[Any] = []
        for i, (key, value) in enumerate(fields.items(), start=2):
            set_clauses.append(f"{key} = ${i}")
            values.append(value)
        query = (
            f"UPDATE call_sessions SET {', '.join(set_clauses)}, updated_at = NOW() "
            "WHERE call_id = $1 RETURNING *"
        )
        row = await conn.fetchrow(query, call_id, *values)
        return dict(row) if row else None


async def list_recent_call_sessions(user_id: int, limit: int = 10) -> list[dict[str, Any]]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "call_sessions"):
            return []
        rows = await conn.fetch(
            "SELECT * FROM call_sessions WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2",
            user_id, limit,
        )
        return _rows(rows)


async def get_usage_count_strict(user_id: int, feature: str, day: Any) -> int:
    """Same as get_usage_count, but RAISES on any real DB-reachability
    failure instead of failing open -- for the one caller in this system
    that needs fail-CLOSED semantics: messa/usage.py's
    peek_usage_monthly_fail_closed, used by call_tools.py's pre-dial
    monthly-minutes check. A DB outage must never silently grant free
    phone-call minutes, unlike every other feature this system meters
    (see get_usage_count's own docstring for why fail-open is the
    deliberately correct default everywhere else -- this function exists
    ONLY because voice calling is the one exception to that).

    Still returns 0 (not a raise) for a pre-migration deployment where the
    table genuinely doesn't exist yet -- that's a real "not set up", not
    an outage, and callers should treat it the same as "no usage yet",
    not block a whole feature over a migration that simply hasn't run."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "usage_daily_counts"):
            return 0
        row = await conn.fetchrow(
            "SELECT count FROM usage_daily_counts WHERE user_id = $1 AND feature = $2 AND day = $3",
            user_id, feature, day,
        )
        return row["count"] if row else 0


# ---------------------------------------------------------------------------
# Active Task Scratchpad + Skills Playbook
# (docs/autonomous_integrations_and_task_memory_spec.md, migration 033).
# Additive tables, same _has_table graceful-degrade shape as projects/
# deepsearch_sessions above: a process running before this migration has
# landed just gets no scratchpad/skills (every function below returns
# None/[] rather than crashing), same as this codebase's every other
# additive feature.
#
# This file stays a "thin repository, no policy" layer per its own module
# docstring: content-safety screening for save_skill (banned phrases,
# length caps, no URLs/credentials -- see the spec doc's security section)
# lives in tools/scratchpad_tools.py, which must call it BEFORE ever
# reaching upsert_skill below. Nothing here second-guesses that -- if it's
# called, it's trusted to already be screened.
# ---------------------------------------------------------------------------

async def get_active_task(user_id: int) -> dict[str, Any] | None:
    """The user's current in_progress/waiting_user_input task, if any -- at
    most one is expected open per user at a time (see start_active_task).
    `artifacts` comes back already json.loads'd into a dict, never a raw
    JSON string, so callers (registry.py's prompt injection, the
    update_task_scratchpad tool) never have to think about the TEXT-column
    storage detail."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "active_tasks"):
            return None
        row = await conn.fetchrow(
            """
            SELECT * FROM active_tasks
            WHERE user_id = $1 AND status IN ('in_progress', 'waiting_user_input')
            ORDER BY updated_at DESC LIMIT 1
            """,
            user_id,
        )
        if not row:
            return None
        d = dict(row)
        try:
            d["artifacts"] = json.loads(d["artifacts"] or "{}")
        except Exception:
            d["artifacts"] = {}
        return d


async def start_active_task(user_id: int, task_type: str) -> dict[str, Any] | None:
    """Opens a new active task, UNLESS the user already has one open, in
    which case that existing row is returned untouched -- "one open task
    per user at a time" is the intended model (spec doc 3.1); a caller that
    genuinely wants to abandon the current one should call
    set_active_task_status(..., 'failed'/'completed') first."""
    existing = await get_active_task(user_id)
    if existing is not None:
        return existing
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "active_tasks"):
            return None
        task_id = str(uuid.uuid4())
        row = await conn.fetchrow(
            """
            INSERT INTO active_tasks (task_id, user_id, task_type, status, artifacts)
            VALUES ($1, $2, $3, 'in_progress', '{}')
            RETURNING *
            """,
            task_id, user_id, task_type,
        )
        d = dict(row)
        d["artifacts"] = {}
        return d


async def update_active_task_artifacts(task_id: str, fields: dict[str, Any]) -> dict[str, Any] | None:
    """Atomically MERGES `fields` into the task's artifacts in a single SQL
    statement -- cast through ::jsonb only for the merge itself
    (COALESCE(artifacts,'{}')::jsonb || $new::jsonb), cast straight back to
    ::text for storage, keeping the column itself TEXT per this project's
    convention (see migration 033's own comment on this exact choice).
    Relies on Postgres's own per-row lock during the UPDATE for atomicity
    -- no read-modify-write race in application code even if two
    subagents/sub-workers touch the same task concurrently, no new asyncpg
    jsonb codec needed. Also bumps updated_at, and flips a
    'waiting_user_input' task back to 'in_progress' -- a fresh artifact
    write is itself evidence real progress resumed."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "active_tasks"):
            return None
        row = await conn.fetchrow(
            """
            UPDATE active_tasks
            SET artifacts = (COALESCE(artifacts, '{}')::jsonb || $2::jsonb)::text,
                updated_at = NOW(),
                status = CASE WHEN status = 'waiting_user_input' THEN 'in_progress' ELSE status END
            WHERE task_id = $1
            RETURNING *
            """,
            task_id, json.dumps(fields),
        )
        if not row:
            return None
        d = dict(row)
        try:
            d["artifacts"] = json.loads(d["artifacts"] or "{}")
        except Exception:
            d["artifacts"] = {}
        return d


async def set_active_task_status(task_id: str, status: str) -> None:
    """status in {'in_progress', 'waiting_user_input', 'completed', 'failed',
    'abandoned'}. The last three are terminal and stamp completed_at, which
    purge_stale_active_tasks (below) uses as its retention clock --
    'abandoned' is what purge_stale_active_tasks itself sets on a task
    nobody ever explicitly completed (see that function's own docstring
    for why this exists: without it, get_active_task would keep injecting
    a long-dead task's artifacts into every future, unrelated conversation
    forever)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "active_tasks"):
            return
        if status in ("completed", "failed", "abandoned"):
            await conn.execute(
                "UPDATE active_tasks SET status = $2, completed_at = NOW(), updated_at = NOW() WHERE task_id = $1",
                task_id, status,
            )
        else:
            await conn.execute(
                "UPDATE active_tasks SET status = $2, updated_at = NOW() WHERE task_id = $1",
                task_id, status,
            )


async def purge_stale_active_tasks() -> dict[str, int]:
    """Retention policy (explicit product decision, not "keep forever" or
    "delete the moment it's done"): a finished task's `artifacts` payload
    -- the actual PII (pitch drafts, recipient emails, spreadsheet IDs) --
    is wiped after config.ACTIVE_TASK_ARTIFACT_RETENTION_DAYS, then the
    whole row is dropped after config.ACTIVE_TASK_ROW_RETENTION_DAYS.
    Called from server.py's _production_scratchpad_cleanup_loop, same
    daily-loop shape as the existing memory batch job (migration 027).

    STEP 0, before any of that: auto-abandon tasks nobody ever explicitly
    completed. tools/scratchpad_tools.py exposes a complete_task tool so an
    agent CAN mark a task done, but an agent forgetting to call it (or a
    conversation that just trails off) must not leave that task
    'in_progress'/'waiting_user_input' forever -- get_active_task has no
    time limit on what counts as "current", so an indefinitely-open task
    would keep getting injected into every future, completely unrelated
    conversation with that user, and would never become eligible for the
    completed/failed purge below either (real bug found in review: the
    original v1 of this function only ever purged 'completed'/'failed'
    rows, and nothing ever transitioned a task OUT of 'in_progress' on its
    own, so in practice no row was ever purged). config.
    ACTIVE_TASK_ABANDON_AFTER_DAYS (default 3 -- deliberately shorter than
    the artifact/row retention windows, since "stop treating this as the
    user's current task" is a much lower bar than "this data is old enough
    to delete") is the cutoff; abandoned tasks then flow through the exact
    same artifact-wipe/row-delete steps as a normally completed one."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "active_tasks"):
            return {"artifacts_purged": 0, "rows_deleted": 0, "abandoned": 0}
        abandon_res = await conn.execute(
            """
            UPDATE active_tasks
            SET status = 'abandoned', completed_at = NOW(), updated_at = NOW()
            WHERE status IN ('in_progress', 'waiting_user_input')
              AND updated_at < NOW() - ($1 || ' days')::interval
            """,
            str(config.ACTIVE_TASK_ABANDON_AFTER_DAYS),
        )
        purge_res = await conn.execute(
            """
            UPDATE active_tasks SET artifacts = '{}'
            WHERE status IN ('completed', 'failed', 'abandoned')
              AND completed_at < NOW() - ($1 || ' days')::interval
              AND artifacts != '{}'
            """,
            str(config.ACTIVE_TASK_ARTIFACT_RETENTION_DAYS),
        )
        delete_res = await conn.execute(
            """
            DELETE FROM active_tasks
            WHERE status IN ('completed', 'failed', 'abandoned')
              AND completed_at < NOW() - ($1 || ' days')::interval
            """,
            str(config.ACTIVE_TASK_ROW_RETENTION_DAYS),
        )

        def _count(res: str) -> int:
            try:
                return int(res.split(" ")[-1])
            except Exception:
                return 0

        return {
            "abandoned": _count(abandon_res),
            "artifacts_purged": _count(purge_res),
            "rows_deleted": _count(delete_res),
        }


async def _evict_excess_skills(conn: asyncpg.Connection, agent_type: str, domain: str) -> None:
    """Caps stored skills per (agent_type, domain) at
    config.SKILLS_MAX_PER_DOMAIN -- without this, a single busy domain
    (e.g. "amazon.com" under deepsearch) could grow unbounded over months.
    Evicts the WEAKEST rows first (lowest success_count, then oldest
    last_used_at), never the strongest.

    ORDER BY ... DESC + OFFSET is deliberate, not a typo: sorting STRONGEST
    first and skipping the first SKILLS_MAX_PER_DOMAIN rows means the
    SELECT returns everything AFTER the cap in that strongest-first
    ordering -- i.e. exactly the weakest excess rows, which the DELETE then
    removes. An earlier version of this query sorted ASC (weakest first)
    with the same OFFSET, which inverted this: OFFSET skipped the weakest
    rows instead and deleted the STRONGEST ones -- caught in code review
    before merge, not by the original unit tests (those only asserted the
    query string contained "DELETE FROM agent_skills", never actual sort
    direction against real row data -- see
    test_scratchpad_and_skills.py's part4_eviction_direction_with_real_rows
    for the regression test that would have caught it).

    Enforced inline on every write (called from upsert_skill), not a
    separate cron job, so the cap holds even if a cleanup job is ever late
    or fails to run."""
    await conn.execute(
        """
        DELETE FROM agent_skills WHERE skill_id IN (
            SELECT skill_id FROM agent_skills
            WHERE agent_type = $1 AND domain = $2
            ORDER BY success_count DESC, last_used_at DESC
            OFFSET $3
        )
        """,
        agent_type, domain, config.SKILLS_MAX_PER_DOMAIN,
    )


async def upsert_skill(
    agent_type: str,
    domain: str,
    problem_pattern: str,
    solution_recipe: str,
    source_user_id: int | None = None,
    source_task_id: str | None = None,
) -> dict[str, Any] | None:
    """INSERT ... ON CONFLICT DO UPDATE on (agent_type, domain,
    problem_pattern) -- the ENTIRE dedup mechanism (see the spec doc's "how
    reliably will this happen" discussion): two agents, or the same one
    twice, discovering the same fix bump success_count/last_used_at on the
    SAME row instead of creating a duplicate -- atomically, no read-then-
    write race, no application-level locking. Caller (tools/scratchpad_
    tools.py's save_skill) is responsible for content-safety screening
    BEFORE calling this -- see this section's own header comment."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "agent_skills"):
            return None
        agent_type = agent_type.lower().strip()
        domain = domain.lower().strip()
        skill_id = str(uuid.uuid4())
        row = await conn.fetchrow(
            """
            INSERT INTO agent_skills
                (skill_id, agent_type, domain, problem_pattern, solution_recipe,
                 success_count, source_user_id, source_task_id)
            VALUES ($1, $2, $3, $4, $5, 1, $6, $7)
            ON CONFLICT (agent_type, domain, problem_pattern) DO UPDATE SET
                solution_recipe = EXCLUDED.solution_recipe,
                success_count = agent_skills.success_count + 1,
                last_used_at = NOW()
            RETURNING *
            """,
            skill_id, agent_type, domain, problem_pattern.strip(), solution_recipe.strip(),
            source_user_id, source_task_id,
        )
        if row is None:
            return None
        result = dict(row)
        await _evict_excess_skills(conn, agent_type, domain)
        return result


async def search_skills(agent_type: str, domain: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Top skills for this exact (agent_type, domain) pair, ranked by
    success_count then recency. This is the read-time half of "how does
    this stay fast as skills grow exponentially": always a narrow, indexed
    lookup (agent_skills_lookup_idx) scoped to one pair, never a full-table
    scan, no matter how many other domains/agent_types exist."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "agent_skills"):
            return []
        rows = await conn.fetch(
            """
            SELECT * FROM agent_skills
            WHERE agent_type = $1 AND domain = $2
            ORDER BY success_count DESC, last_used_at DESC
            LIMIT $3
            """,
            agent_type.lower().strip(), domain.lower().strip(),
            limit or config.SKILLS_MAX_PER_QUERY,
        )
        return _rows(rows)


# ---------------------------------------------------------------------------
# Persistent Workspace Asset Registry + per-toolkit Entity cache
# (docs/executive_agent_architecture_proposal.md sections 3.A/3.B,
# migration 037/workspace_asset_registry.sql). See that migration's own
# header comment for the full dedup reasoning. SERIAL-id, upsert-per-user
# shape -- same convention as user_app_preferences above, NOT the UUID
# convention active_tasks/agent_skills use just above this.
# ---------------------------------------------------------------------------

async def record_user_asset(
    user_id: int,
    asset_type: str,
    title: str,
    external_id: str | None,
    url: str | None,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Upsert when external_id is known: re-touching an already-known
    (user_id, asset_type, external_id) asset refreshes title/url/summary
    and bumps last_referenced_at on the SAME row rather than creating a
    duplicate. When external_id is None (a URL-only discovery -- see
    asset_consolidation.py's background sweep), this always INSERTs a new
    row instead -- Postgres's own UNIQUE-constraint semantics mean NULL
    never collides against NULL, which is also the right real-world
    behavior here: two different unknown-id discoveries of the same type/
    title aren't necessarily the same asset touched twice."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "user_assets"):
            return None
        if external_id:
            row = await conn.fetchrow(
                """
                INSERT INTO user_assets (user_id, asset_type, title, external_id, url, summary)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (user_id, asset_type, external_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    url = COALESCE(EXCLUDED.url, user_assets.url),
                    summary = COALESCE(EXCLUDED.summary, user_assets.summary),
                    last_referenced_at = NOW()
                RETURNING *
                """,
                user_id, asset_type, title, external_id, url, summary,
            )
        else:
            row = await conn.fetchrow(
                """
                INSERT INTO user_assets (user_id, asset_type, title, external_id, url, summary)
                VALUES ($1, $2, $3, NULL, $4, $5)
                RETURNING *
                """,
                user_id, asset_type, title, url, summary,
            )
        return dict(row) if row else None


async def get_recent_user_assets(user_id: int, limit: int | None = None) -> list[dict[str, Any]]:
    """The read-time half of "never have amnesia about past assets" --
    registry._build_system_prompt injects this list (capped at
    config.USER_ASSETS_MAX_INJECTED) into every turn's system prompt, the
    same "small, fixed, always-there" shape as the known-about-user block
    right next to it."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "user_assets"):
            return []
        rows = await conn.fetch(
            """
            SELECT * FROM user_assets WHERE user_id = $1
            ORDER BY last_referenced_at DESC LIMIT $2
            """,
            user_id, limit or config.USER_ASSETS_MAX_INJECTED,
        )
        return _rows(rows)


async def touch_user_asset_by_url(user_id: int, url: str) -> bool:
    """Bumps last_referenced_at on an already-known asset matching this
    exact URL, WITHOUT creating a new row. Used by asset_consolidation.py's
    background sweep to avoid double-recording a link a synchronous hook
    (execute_integration_tool/document_tools' PDF tools) already persisted
    earlier in the very same turn. Returns whether a row actually existed
    to touch, so the caller knows whether it still needs to record a
    brand-new row for this URL."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "user_assets"):
            return False
        result = await conn.execute(
            "UPDATE user_assets SET last_referenced_at = NOW() WHERE user_id = $1 AND url = $2",
            user_id, url,
        )
        try:
            return int(result.split(" ")[-1]) > 0
        except Exception:
            return False


async def purge_stale_user_assets() -> dict[str, int]:
    """Retention sweep: drops rows not referenced in
    config.USER_ASSETS_ROW_RETENTION_DAYS -- same time-window pattern as
    purge_stale_active_tasks above, run from server.py's
    _production_scratchpad_cleanup_loop (extended to cover this table
    too)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "user_assets"):
            return {"rows_deleted": 0}
        result = await conn.execute(
            """
            DELETE FROM user_assets
            WHERE last_referenced_at < NOW() - ($1 || ' days')::interval
            """,
            str(config.USER_ASSETS_ROW_RETENTION_DAYS),
        )
        try:
            deleted = int(result.split(" ")[-1])
        except Exception:
            deleted = 0
        return {"rows_deleted": deleted}


async def get_cached_app_entity(user_id: int, toolkit_slug: str, entity_type: str) -> str | None:
    """The one cached id (a default Airtable workspace, a default Drive
    folder, ...) execute_integration_tool auto-injects instead of either
    asking the user for it or letting the model guess/fabricate one. None
    if never discovered (or discovery failed) -- callers must degrade
    gracefully to today's ask-or-guess-free behavior in that case, never
    treat a cache miss as an error."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "user_app_entities"):
            return None
        row = await conn.fetchrow(
            "SELECT entity_id FROM user_app_entities WHERE user_id = $1 AND toolkit_slug = $2 AND entity_type = $3",
            user_id, toolkit_slug.lower().strip(), entity_type,
        )
        return row["entity_id"] if row else None


async def set_cached_app_entity(
    user_id: int, toolkit_slug: str, entity_type: str, entity_id: str, label: str | None = None,
) -> dict[str, Any] | None:
    """Upsert -- a second discovery for the same (user, toolkit, type)
    (e.g. the user reconnected the app) overwrites the stale id rather
    than creating a duplicate row, same ON CONFLICT DO UPDATE shape as
    set_app_preference above."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        if not await _has_table(conn, "user_app_entities"):
            return None
        row = await conn.fetchrow(
            """
            INSERT INTO user_app_entities (user_id, toolkit_slug, entity_type, entity_id, label)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (user_id, toolkit_slug, entity_type) DO UPDATE SET
                entity_id = EXCLUDED.entity_id, label = EXCLUDED.label, discovered_at = NOW()
            RETURNING *
            """,
            user_id, toolkit_slug.lower().strip(), entity_type, entity_id, label,
        )
        return dict(row) if row else None
