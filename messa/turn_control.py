"""In-memory, read-only awareness of "is another turn already running for
this user right now" -- built for Feature B2b of the smart-tapbacks-and-
double-texting round (plans/glowing-forging-pumpkin.md): when a follow-up
text arrives well after a turn already started (a user checking in, or
correcting something, minutes into a deepsearch delegation), the model
should know that turn is still in flight instead of guessing blind.

Deliberately separate from deepsearch_control.py, which stays untouched --
that module tracks ONE specific kind of in-flight work (a deepsearch
browser session) and supports actually CANCELLING it. This module tracks
ANY in-flight top-level turn (not just deepsearch) and is deliberately
read-only: it cannot be a cancellation mechanism, because cli.run_message
rebuilds the orchestrator and reconstructs conversation history from the
DB fresh on every single call, with no LangGraph checkpointer/thread_id
carried across turns -- there is no live object a second message could
actually reach into and interrupt. Promising the model "you can stop that"
here would be a lie; describe() below says only that a turn IS running,
and the prompt block built from it (see agents/registry.py) is explicit
that a correction can't reach back into it.

A user can have more than one turn in flight at once (double-texting is
exactly the scenario this exists for), hence a per-user dict of
token -> entry rather than a single slot. Non-durable, in-process only --
gone on restart, same as deepsearch_control.py -- with a generous
staleness prune as a leak safety net in case a killed process ever leaves
an entry dangling with no matching end_turn() call."""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass

# Generous on purpose -- this only exists to catch a process that got
# killed mid-turn and never reached its own finally/end_turn() call. A
# legitimately long-running turn (a multi-minute deepsearch delegation)
# should never come close to this.
_STALE_AFTER_SECONDS = 15 * 60
# Long enough to be recognizable in the prompt block below, short enough
# to never meaningfully bloat it.
_PREVIEW_CHARS = 120

_next_token = itertools.count(1)


@dataclass
class _InFlightTurn:
    preview: str
    started_at: float


_in_flight: dict[int, dict[int, _InFlightTurn]] = {}


def _prune(user_id: int) -> None:
    """Drops any entry older than _STALE_AFTER_SECONDS. A safety net for a
    process crash/kill that skips the matching end_turn() call (normally
    invoked from a `finally`, so in practice this should rarely if ever
    actually find anything to prune)."""
    entries = _in_flight.get(user_id)
    if not entries:
        return
    now = time.monotonic()
    stale = [token for token, entry in entries.items() if now - entry.started_at > _STALE_AFTER_SECONDS]
    for token in stale:
        entries.pop(token, None)
    if not entries:
        _in_flight.pop(user_id, None)


def start_turn(user_id: int, preview: str) -> int:
    """Called right after build_orchestrator returns for a turn (see
    server.py's _process_inbound) -- deliberately AFTER, not before:
    agents/registry.py's own system-prompt builder reads describe() for
    THIS SAME user while constructing THIS SAME turn's prompt, so a turn
    must never be able to describe itself as already in flight. Returns an
    opaque token; pass it back to end_turn to remove exactly this entry --
    a user can have more than one turn in flight at once (that's exactly
    the double-texting scenario this module exists for)."""
    _prune(user_id)
    token = next(_next_token)
    preview = (preview or "").strip().replace("\n", " ")
    if len(preview) > _PREVIEW_CHARS:
        preview = preview[:_PREVIEW_CHARS].rstrip() + "..."
    _in_flight.setdefault(user_id, {})[token] = _InFlightTurn(preview=preview, started_at=time.monotonic())
    return token


def end_turn(user_id: int, token: int | None = None) -> None:
    """Called from _process_inbound's own `finally`, so this always fires
    regardless of how the turn ended (clean finish, exception, or a
    cancellation). Token-scoped so ending one turn never accidentally
    clears a DIFFERENT still-running turn for the same user."""
    entries = _in_flight.get(user_id)
    if not entries:
        return
    if token is not None:
        entries.pop(token, None)
    if not entries:
        _in_flight.pop(user_id, None)


def is_active(user_id: int) -> bool:
    _prune(user_id)
    return bool(_in_flight.get(user_id))


def active_count(user_id: int) -> int:
    _prune(user_id)
    entries = _in_flight.get(user_id)
    return len(entries) if entries else 0


def _humanize(seconds: float) -> str:
    """A short, human "X ago" string for the prompt block below -- doesn't
    need to be precise, just legible ("just now", "45 seconds ago", "3
    minutes ago")."""
    seconds = max(0.0, seconds)
    if seconds < 5:
        return "just now"
    if seconds < 60:
        return f"{int(seconds)} seconds ago"
    minutes = int(seconds // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''} ago"


def describe(user_id: int) -> str | None:
    """A short human-readable description of the OLDEST still-running turn
    for this user (the one most likely to be what a follow-up is actually
    about), or None if nothing is in flight. Consumed by agents/registry.py
    to build its own in-flight-awareness prompt block -- this function
    itself has no opinion on what the model should DO about it, see this
    module's own docstring for why."""
    _prune(user_id)
    entries = _in_flight.get(user_id)
    if not entries:
        return None
    oldest = min(entries.values(), key=lambda e: e.started_at)
    elapsed = _humanize(time.monotonic() - oldest.started_at)
    if oldest.preview:
        return f"\"{oldest.preview}\", sent {elapsed}"
    return f"a message sent {elapsed}"
