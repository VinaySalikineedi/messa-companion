"""In-memory, per-user "what is deepsearch doing right now" log, feeding the
live-view page's description line and chain-of-thought panel (see
live_view_page.py and tools/deepsearch_tools.py's guarded tool calls).

Deliberately NOT persisted to Postgres, unlike live_view_url/live_share_token
in db.py: this is scoped to exactly one in-flight deepsearch delegation and
already has a natural clear point (the same try/finally in
tools/deepsearch_tools.py's `_run()` that flips `live_browser_active` off
when the browser closes, success or failure alike). If the Space process
restarts mid-task, this is lost -- but so is the actual browser session, so
there's nothing meaningful left to show anyway; the live-view page's own
15-minute staleness cutoff (db.get_live_status_by_token) is the real safety
net for a browser that's gone but the DB doesn't know it yet.

Keyed by user_id, not the public live-share token: tools/deepsearch_tools.py
only has user_id in scope when it's making guarded tool calls.
server.py's /live/<token>/status route resolves token -> user_id via
db.get_live_status_by_token (see that function's "id" column) before
reading this module.
"""
from __future__ import annotations

# Chain-of-thought lines are for one task's live viewing, not a permanent
# transcript -- capped so a very long-running deepsearch run can't grow this
# unbounded in memory or make the log panel scroll forever.
MAX_STEPS = 60

_state: dict[int, dict] = {}


_DEFAULT_ENTRY = {
    "description": None, "steps": [], "closing": False, "waiting_for_human": None, "tabs": {},
    # active_tab_id/url/bb_session_id: added for the "follow whichever tab is
    # actually active" live-view feature (server.py's /live/<token>/status).
    # active_tab_id is None while the TOP-LEVEL tab is the one most recently
    # doing something, or a sub-worker's own tab_id -- updated on every
    # guarded() call (see tools/deepsearch_tools.py's _live_mark_active), so
    # it always reflects whichever tab most recently acted, a reasonable
    # proxy for "the one you'd want to be watching" when several run
    # concurrently. url mirrors the same thing for the top-level tab (a
    # sub-worker's current url already lives on its own entry in `tabs`).
    # bb_session_id is the raw Browserbase session id for this run, needed
    # to re-poll GET /sessions/{id}/debug for a specific tab's OWN debug URL
    # (channels/browserbase.get_session_pages) -- not persisted to Postgres,
    # same reasoning as everything else in this module (see module docstring).
    "active_tab_id": None, "url": None, "bb_session_id": None,
}


def start(user_id: int, initial_description: str | None) -> None:
    """Called once when a deepsearch delegation's browser opens."""
    _state[user_id] = {
        "description": initial_description or "Working on it...", "steps": [], "closing": False,
        "waiting_for_human": None, "tabs": {},
        "active_tab_id": None, "url": None, "bb_session_id": None,
    }


def set_description(user_id: int, text: str) -> None:
    """Updates the one-line "doing this right now" text shown above the video."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["description"] = text


def add_step(user_id: int, text: str) -> None:
    """Appends one completed action to the chain-of-thought log below the video."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["steps"].append(text)
    if len(entry["steps"]) > MAX_STEPS:
        entry["steps"] = entry["steps"][-MAX_STEPS:]


def set_closing(user_id: int) -> None:
    """Called once the browser session is about to be released -- while the
    DB still says a browser is live (tools/deepsearch_tools.py sets this
    *before* leaving BrowserToolProvider's `async with` block, i.e. before
    the Browserbase session is actually released) so the live-view page's
    next poll can proactively swap to a "Compiling your results..." screen
    instead of showing whatever Browserbase's own embedded debug page
    renders the instant its CDP connection is torn down (a raw
    "Debugging connection was closed" banner)."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["closing"] = True


def set_waiting_for_human(user_id: int, reason: str) -> None:
    """Called by tools/deepsearch_tools.py's request_human_help the instant
    it starts waiting -- lets the live-view page show a "Messa is waiting
    for you" banner (reason included) instead of looking like the task
    simply stalled. Cleared by clear_waiting_for_human once the wait
    resolves or times out, whichever comes first -- never left set past the
    end of that specific wait."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["waiting_for_human"] = reason


def clear_waiting_for_human(user_id: int) -> None:
    entry = _state.get(user_id)
    if entry is not None:
        entry["waiting_for_human"] = None


def set_session_id(user_id: int, bb_session_id: str) -> None:
    """Called once by the owning/top-level BrowserToolProvider right after
    it creates its Browserbase session -- lets server.py's status route
    re-poll GET /sessions/{id}/debug later to resolve a specific tab's own
    per-page debug URL (see get_active_live_view_url below)."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["bb_session_id"] = bb_session_id


def set_url(user_id: int, url: str) -> None:
    """Top-level tab's current url -- mirrors the `url` field each entry in
    `tabs` already carries for a sub-worker."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["url"] = url


def set_tab_url(user_id: int, tab_id: str, url: str) -> None:
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    tab = entry.setdefault("tabs", {}).get(tab_id)
    if tab is not None:
        tab["url"] = url


def set_active_tab(user_id: int, tab_id: str | None) -> None:
    """Called on every guarded() tool call (top-level and every sub-worker
    alike) with that call's own tab_id (None for the top-level tab) -- so
    this always names whichever tab most recently did something, the proxy
    server.py's status route uses to decide which tab's live-view debug URL
    to show right now (see that route for the full "follow the active tab"
    logic)."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["active_tab_id"] = tab_id


# ---------------------------------------------------------------------------
# Per-tab state (multi-site delegation -- see tools/deepsearch_tools.py's
# delegate_website_task). Deliberately a SEPARATE dict from the flat fields
# above rather than folding the top-level/orchestrating tab into it too:
# several sub-worker tabs can be waiting on request_human_help at the same
# time, and each must be able to set/clear its own waiting_for_human reason
# without racing to clobber another tab's (or the top-level's) entry the way
# a single shared flag would. The flat fields above keep meaning exactly what
# they meant before multi-site delegation existed -- the orchestrating tab's
# own status; this `tabs` dict is purely additive, one entry per
# concurrently-delegated sub-worker, keyed by that worker's tab_id.
# ---------------------------------------------------------------------------


def set_tab(user_id: int, tab_id: str, url: str | None) -> None:
    """Called once when a delegate_website_task sub-worker's tab opens."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry.setdefault("tabs", {})[tab_id] = {
        "url": url, "description": None, "steps": [], "waiting_for_human": None,
    }


def set_tab_description(user_id: int, tab_id: str, text: str) -> None:
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    tab = entry.setdefault("tabs", {}).get(tab_id)
    if tab is not None:
        tab["description"] = text


def add_tab_step(user_id: int, tab_id: str, text: str) -> None:
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    tab = entry.setdefault("tabs", {}).get(tab_id)
    if tab is not None:
        tab["steps"].append(text)
        if len(tab["steps"]) > MAX_STEPS:
            tab["steps"] = tab["steps"][-MAX_STEPS:]


def set_tab_waiting_for_human(user_id: int, tab_id: str, reason: str) -> None:
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    tab = entry.setdefault("tabs", {}).get(tab_id)
    if tab is not None:
        tab["waiting_for_human"] = reason


def clear_tab_waiting_for_human(user_id: int, tab_id: str) -> None:
    entry = _state.get(user_id)
    if entry is not None:
        tab = entry.get("tabs", {}).get(tab_id)
        if tab is not None:
            tab["waiting_for_human"] = None


def clear_tab(user_id: int, tab_id: str) -> None:
    """Called once a delegate_website_task sub-worker's tab closes (its
    async with block ending, success or failure alike) -- so the live-view
    page never shows a finished sub-worker as still in progress."""
    entry = _state.get(user_id)
    if entry is not None:
        entry.get("tabs", {}).pop(tab_id, None)


def get(user_id: int) -> dict:
    """Returns {"description": str|None, "steps": list[str], "closing": bool,
    "waiting_for_human": str|None, "tabs": {tab_id: {...}}, "active_tab_id":
    str|None, "url": str|None, "bb_session_id": str|None} -- never raises,
    just gives back an empty/not-closing/not-waiting/no-tabs log for a user
    with nothing currently tracked."""
    entry = _state.get(user_id)
    if entry is None:
        return dict(_DEFAULT_ENTRY, tabs={})
    return {
        "description": entry["description"],
        "steps": list(entry["steps"]),
        "closing": bool(entry.get("closing")),
        "waiting_for_human": entry.get("waiting_for_human"),
        "tabs": {k: dict(v) for k, v in entry.get("tabs", {}).items()},
        "active_tab_id": entry.get("active_tab_id"),
        "url": entry.get("url"),
        "bb_session_id": entry.get("bb_session_id"),
    }


def clear(user_id: int) -> None:
    """Called once when a deepsearch delegation's browser closes (try/finally,
    so this fires whether the run finished cleanly, hit its step limit, or
    errored) -- so the live-view page never shows stale chain-of-thought from
    a task that's already done."""
    _state.pop(user_id, None)
