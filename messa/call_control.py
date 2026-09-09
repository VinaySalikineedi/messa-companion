"""In-memory bookkeeping of "which call_sessions row is this user's active
outbound call right now" -- the voice-calling counterpart to
deepsearch_control.py, but NOT a copy of its cancellation mechanism.

Why this is different from deepsearch_control.py: a deepsearch delegation
is a live asyncio.Task running IN THIS PROCESS, so deepsearch_control can
hold a real task handle and literally .cancel() it. A phone call runs
entirely on Vapi's own infrastructure -- once vapi.create_call() returns,
this process has no running task to cancel at all; the call only ever
reaches us again via a webhook (mid-call tool-call, end-of-call report).
"Hang up" is therefore a call to vapi.end_call() (a tool the orchestrator
invokes, see messa/tools/call_tools.py), never a method on this module.

What this module IS for: the per-user concurrency cap
(config.CALL_MAX_CONCURRENT_PER_USER) and a "is this user already on a
call" check, both of which need a live view of in-flight calls without a
DB round-trip on every single check. Backed by db.call_sessions as the
source of truth (register/unregister mirror that table's own state
transitions) -- this module is a fast in-memory cache in front of it, not
a second source of truth: if the process restarts, this cache is empty,
but db.get_active_call_session_for_user still has the real row, and the
staleness GC below exists specifically to reconcile "the DB thinks a call
is still active but nothing ever closed it out" (e.g. this process died
mid-call, or an end-of-call webhook never arrived) so a lost slot doesn't
stay stuck open forever.
"""
from __future__ import annotations

import time

# user_id -> {call_id: (registered_at monotonic ts, that call's own
# max_duration_seconds)} -- the max_duration is captured PER CALL (not a
# single global constant) since call_tools.py's dial_confirmed_call already
# computes a call-specific cap (min(config.CALL_MAX_DURATION_SECONDS,
# remaining_monthly_minutes * 60)), and staleness pruning needs that same
# per-call number to know how long a registration should be trusted.
_active_calls: dict[int, dict[str, tuple[float, int]]] = {}

# Mirrors db.LIVE_VIEW_STALE_AFTER's reasoning, but call-specific: a call
# is allowed to run for at most its own max_duration_seconds (enforced by
# Vapi itself), plus this grace window for the end-of-call-report webhook
# to actually arrive and call unregister(). If that webhook is late or
# never arrives (a dropped webhook, a process restart), a slot must not
# stay pinned "active" forever -- see _prune_stale below, run before every
# read in this module so a stale entry is never trusted.
STALE_GRACE_SECONDS = 120


def register(user_id: int, call_id: str, max_duration_seconds: int) -> None:
    """Called right after dial_confirmed_call successfully places the call
    (i.e. after vapi.create_call() returns, not at propose_call/confirm
    time -- a call that's merely confirmed-but-not-yet-dialed doesn't
    occupy a concurrency slot)."""
    _active_calls.setdefault(user_id, {})[call_id] = (time.monotonic(), max_duration_seconds)


def unregister(user_id: int, call_id: str) -> None:
    """Called on every call-ending path (end-of-call-report webhook,
    error handling in dial_confirmed_call, a manual hangup) -- safe to
    call even if the call_id was never registered or already removed."""
    calls = _active_calls.get(user_id)
    if calls is not None:
        calls.pop(call_id, None)
        if not calls:
            _active_calls.pop(user_id, None)


def _prune_stale(user_id: int) -> None:
    calls = _active_calls.get(user_id)
    if not calls:
        return
    now = time.monotonic()
    fresh = {
        cid: (ts, max_dur) for cid, (ts, max_dur) in calls.items()
        if (now - ts) <= (max_dur + STALE_GRACE_SECONDS)
    }
    if fresh:
        _active_calls[user_id] = fresh
    else:
        _active_calls.pop(user_id, None)


def is_active(user_id: int) -> bool:
    _prune_stale(user_id)
    return bool(_active_calls.get(user_id))


def active_count(user_id: int) -> int:
    """The number this module exists for: config.CALL_MAX_CONCURRENT_PER_USER
    is compared against this in build_call_subagent's gating (messa/tools/
    call_tools.py) BEFORE a new call is ever proposed."""
    _prune_stale(user_id)
    return len(_active_calls.get(user_id, {}))


def describe(user_id: int) -> list[str]:
    """The call_ids currently believed active for this user -- mainly for
    debugging/admin visibility, not used in any gating decision itself
    (active_count/is_active already apply the staleness prune themselves)."""
    _prune_stale(user_id)
    return list(_active_calls.get(user_id, {}).keys())


def clear_all_for_test() -> None:
    """Test-only reset -- this module is pure process-global state, so
    tests that run in the same process must be able to reset it between
    cases rather than leaking registrations across unrelated test
    functions."""
    _active_calls.clear()
