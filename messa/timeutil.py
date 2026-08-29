"""Timezone resolution and time-correctness helpers.

Root fix for a bug reported directly: "the llm works in UTC and we show
results to the user in whatever timezone they are, without proper setup
there will be confusion." Tracing that down turned up two independent,
compounding causes, both fixed here:

  1. WRITE side: `db._parse_dt` (the old, now-removed version of this
     module's logic) took a naive datetime string from the model -- exactly
     what "3pm tomorrow" looks like once the model writes it into a tool
     call -- and silently assumed it was already UTC. A user in Pacific
     time asking for "3pm" got it stored as 3pm UTC (7am/8am Pacific),
     with nothing anywhere flagging the mismatch.
  2. READ side: displaying a stored (UTC) timestamp back to the user just
     stringified whatever asyncpg handed back, with no conversion to the
     user's own timezone and no zone label -- so even a *correctly stored*
     time looked wrong once shown.
  3. Underlying both: `users.timezone` was set once, at account creation,
     to a hardcoded default (config.DEFAULT_TIMEZONE) and never derived
     from the city the user actually gives during onboarding -- so even
     with (1) and (2) fixed, the timezone being used could still just be
     wrong for that specific user.

This module fixes all three: `resolve_timezone` turns a real city/zip into
an IANA timezone; `to_local_aware` interprets a naive datetime as the
user's own local time (not UTC) before it's ever written to the database;
`format_local` renders a stored timestamp back in that same local time with
an explicit zone label; `current_context_str` gives every system prompt an
explicit, unambiguous "here is the actual current date/time and timezone"
anchor, since none of them had one before.

Geocoding: Open-Meteo's free, keyless geocoding API
(https://geocoding-api.open-meteo.com/v1/search) resolves both city names
and US zip codes to an IANA timezone directly, no separate lat/lon->tz
lookup needed. Verified live (three manual fetches during development --
see the README) including a real disambiguation hazard: a bare city name
with no state ("Jacksonville") returns results in three different US
timezones, and even "City, State" can include a low-population outlier
(e.g. a heliport) tagged with a different, wrong-for-the-city timezone --
handled below by preferring populated results and checking whether the
highest-population candidates actually agree before calling a resolution
"confident."

Sandbox note: this project's own dev sandbox restricts outbound network
calls to a small package-registry allowlist, so the live API call itself
could only be verified by hand, not from an automated test in that
environment -- the decision logic here (population weighting, agreement
checking, zip vs. city handling) is covered by unit tests against mocked
API responses instead. Any normal deployment target (HuggingFace Spaces
included) has unrestricted outbound HTTPS, so the real call is expected to
work as designed once deployed -- flagging this as the one thing that
genuinely could not be end-to-end verified from within this sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx
from dateutil import parser as dateutil_parser

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
GEOCODING_TIMEOUT_SECONDS = 8.0


@dataclass
class TimezoneResolution:
    timezone: str
    resolved_name: str  # human-readable, e.g. "Jacksonville, Florida, US"
    confident: bool  # False when the top candidates disagree on timezone


async def resolve_timezone(location: str) -> Optional[TimezoneResolution]:
    """Resolve a free-text city (ideally 'City, ST' or 'City, Country') or a
    US zip code to an IANA timezone via Open-Meteo's geocoding search.
    Returns None on any lookup failure (network error, no results, no
    timezone field) rather than raising -- callers should treat that as
    "couldn't resolve, ask the user to be more specific," not a hard error.
    """
    location = (location or "").strip()
    if not location:
        return None
    try:
        async with httpx.AsyncClient(timeout=GEOCODING_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                GEOCODING_URL,
                params={"name": location, "count": 10, "language": "en", "format": "json"},
            )
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    return _pick_resolution(data.get("results") or [])


def _pick_resolution(results: list[dict[str, Any]]) -> Optional[TimezoneResolution]:
    """Pure decision logic, split out from the network call so it can be
    unit-tested directly against recorded/mocked API responses."""
    if not results:
        return None

    # Prefer populated places over degenerate/tiny features (a heliport, a
    # park) that can share a name with a real city but carry an unrelated
    # or simply wrong timezone tag -- verified live: a bare "Jacksonville,
    # FL" search returns exactly this kind of outlier alongside several
    # normal, populous results.
    populated = [r for r in results if (r.get("population") or 0) > 0]
    candidates = populated or results
    candidates = sorted(candidates, key=lambda r: r.get("population") or 0, reverse=True)

    top = candidates[0]
    tz_name = top.get("timezone")
    if not tz_name:
        return None

    # Confidence: do the highest-population candidates actually agree on
    # timezone? A bare "Jacksonville" (no state) genuinely matches cities in
    # three different US timezones -- still return the best guess (highest
    # population), but flag it as unconfident so the caller can ask the
    # user to clarify rather than silently trusting a guess for something
    # as consequential as when a reminder fires.
    top_n = candidates[:5]
    distinct_tz = {r.get("timezone") for r in top_n if r.get("timezone")}
    confident = len(distinct_tz) <= 1

    name_parts = [p for p in (top.get("name"), top.get("admin1"), top.get("country")) if p]
    resolved_name = ", ".join(name_parts) if name_parts else tz_name

    return TimezoneResolution(timezone=tz_name, resolved_name=resolved_name, confident=confident)


def to_local_aware(raw: Any, user_tz: str) -> Optional[datetime]:
    """Parse a datetime the way a model actually produces it (an ISO-ish
    string, an already-aware/naive datetime, or a relative phrase like
    "tomorrow" / "tomorrow at 3pm") and, if it carries no explicit UTC
    offset, interpret it as the user's LOCAL time (per user_tz) instead of
    assuming UTC -- this is the core fix for the write-side half of the
    "LLM works in UTC" bug.

    Returns a tz-aware datetime (asyncpg/Postgres store it correctly as UTC
    internally regardless of which aware zone it's given), or None for an
    empty/None input. Raises ValueError if the string genuinely can't be
    parsed at all, matching the previous behavior callers already handle.
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=ZoneInfo(user_tz))
        return raw

    text = str(raw).strip()
    if not text:
        return None

    tz = ZoneInfo(user_tz)

    import re
    tz_match = re.search(r"\s+([A-Za-z_]+/[A-Za-z_]+)$", text)
    if tz_match:
        try:
            tz = ZoneInfo(tz_match.group(1))
            text = text[:tz_match.start()].strip()
        except Exception:  # noqa: BLE001
            pass

    if text.endswith("Z"):
        # Explicit UTC marker -- the model (or an internal caller) meant
        # UTC on purpose here, so respect it as given rather than
        # reinterpreting it as local time.
        return datetime.fromisoformat(text[:-1] + "+00:00")

    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            return dt  # explicit offset given -- trust it as-is
        return dt.replace(tzinfo=tz)  # naive -- this IS the user's local time
    except ValueError:
        pass

    now_local = datetime.now(tz)
    lower = text.lower()
    if lower == "today":
        return now_local
    if lower == "tomorrow":
        return now_local + timedelta(days=1)

    base = now_local
    if "tomorrow" in lower:
        base = now_local + timedelta(days=1)
        text_clean = lower.replace("tomorrow", "").replace("noon", "12:00 PM").strip()
    else:
        text_clean = lower.replace("today", "").replace("noon", "12:00 PM").strip()

    try:
        dt = dateutil_parser.parse(text_clean, default=base) if text_clean else base
    except Exception as e:
        raise ValueError(f"Could not parse datetime string: {raw!r}") from e
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def format_local(dt: Optional[datetime], user_tz: str) -> str:
    """Render a stored (UTC-aware, or any aware/naive) datetime in the
    user's own local time with an explicit zone abbreviation, e.g.
    'Aug 27, 2026 03:00 PM EDT' -- fixes the read-side half of the "LLM
    works in UTC" bug, where a raw UTC timestamp was shown with no
    conversion and no label at all."""
    if dt is None:
        return "(no time set)"
    tz = ZoneInfo(user_tz)
    local_dt = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc).astimezone(tz)
    return local_dt.strftime("%b %d, %Y %I:%M %p %Z")


def current_context_str(user_tz: str, confirmed: bool) -> str:
    """One line every time-sensitive system prompt should include: the
    actual current date/time in the user's own timezone, made explicit
    rather than left for the model to guess or assume UTC. `confirmed`
    (users.timezone_confirmed) marks whether this timezone was actually
    derived from something the user told us -- when it's False, the
    returned string tells the model to confirm a city/zip before treating
    anything time-sensitive as settled."""
    now_utc = datetime.now(dt_timezone.utc)
    tz = ZoneInfo(user_tz)
    now_local = now_utc.astimezone(tz)
    label = now_local.strftime("%A, %B %d, %Y %I:%M %p %Z")
    base = f"Current date/time: {label} (timezone: {user_tz})."
    if confirmed:
        return base
    return (
        base + " This timezone is an UNCONFIRMED default, not something the user actually "
        "told us -- if this request involves a specific time, date, or anything "
        "time-sensitive (scheduling, a reminder, a due date), ask for their city or zip "
        "code first so it lands at the correct local time, then save it via "
        "save_profile_info('city', ...) before proceeding."
    )
