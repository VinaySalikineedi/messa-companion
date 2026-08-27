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


def start(user_id: int, initial_description: str | None) -> None:
    """Called once when a deepsearch delegation's browser opens."""
    _state[user_id] = {"description": initial_description or "Working on it...", "steps": []}


def set_description(user_id: int, text: str) -> None:
    """Updates the one-line "doing this right now" text shown above the video."""
    entry = _state.setdefault(user_id, {"description": None, "steps": []})
    entry["description"] = text


def add_step(user_id: int, text: str) -> None:
    """Appends one completed action to the chain-of-thought log below the video."""
    entry = _state.setdefault(user_id, {"description": None, "steps": []})
    entry["steps"].append(text)
    if len(entry["steps"]) > MAX_STEPS:
        entry["steps"] = entry["steps"][-MAX_STEPS:]


def get(user_id: int) -> dict:
    """Returns {"description": str|None, "steps": list[str]} -- never raises,
    just gives back an empty log for a user with nothing currently tracked."""
    entry = _state.get(user_id)
    if entry is None:
        return {"description": None, "steps": []}
    return {"description": entry["description"], "steps": list(entry["steps"])}


def clear(user_id: int) -> None:
    """Called once when a deepsearch delegation's browser closes (try/finally,
    so this fires whether the run finished cleanly, hit its step limit, or
    errored) -- so the live-view page never shows stale chain-of-thought from
    a task that's already done."""
    _state.pop(user_id, None)
