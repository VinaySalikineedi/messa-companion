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
}


def start(user_id: int, initial_description: str | None) -> None:
    """Called once when a deepsearch delegation's browser opens."""
    _state[user_id] = {
        "description": initial_description or "Working on it...", "steps": [], "closing": False,
        "waiting_for_human": None, "tabs": {},
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
    "waiting_for_human": str|None, "tabs": {tab_id: {...}}} -- never raises,
    just gives back an empty/not-closing/not-waiting/no-tabs log for a user
    with nothing currently tracked."""
    entry = _state.get(user_id)
    if entry is None:
        return {**dict(_DEFAULT_ENTRY), "tabs": {}}
    return {
        "description": entry["description"],
        "steps": list(entry["steps"]),
        "closing": bool(entry.get("closing")),
        "waiting_for_human": entry.get("waiting_for_human"),
        "tabs": {k: dict(v) for k, v in entry.get("tabs", {}).items()},
    }


def clear(user_id: int) -> None:
    """Called once when a deepsearch delegation's browser closes (try/finally,
    so this fires whether the run finished cleanly, hit its step limit, or
    errored) -- so the live-view page never shows stale chain-of-thought from
    a task that's already done."""
    _state.pop(user_id, None)
