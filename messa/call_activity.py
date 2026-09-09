"""In-memory, per-user "what is this call doing right now" log -- the
voice-calling counterpart to live_activity.py, feeding the call-specific
live-listen page (server.py's GET /live/{token}/call and
/live/{token}/call/status).

Deliberately NOT persisted to Postgres, same reasoning as live_activity.py's
own module docstring: this is scoped to exactly one in-flight call and
already has a natural clear point (the end-of-call-report webhook, or an
error path in dial_confirmed_call). If the process restarts mid-call, this
is lost -- but so is any real ability to keep relaying that call's audio,
so there's nothing meaningful left to show anyway.

Keyed by user_id, matching call_control.py and live_activity.py's own
convention -- messa/tools/call_tools.py and the webhook handler both
already have user_id in scope by the time they touch this (resolved via
db.get_call_session/get_call_session_by_provider_id).

set_listen_url is the one function that matters most for this feature's
own security design: Vapi's listenUrl is held ONLY here, in memory, never
written to call_sessions (see migrations/035_call_sessions.sql's own
"deliberate omissions" comment) and never sent to the browser directly --
server.py's audio-relay WebSocket route reads it from here to open its OWN
outbound connection to Vapi, then relays frames to the browser. Losing it
on a restart is fine: the call itself would need re-establishing anyway.
"""
from __future__ import annotations

MAX_TRANSCRIPT_LINES = 200

_state: dict[int, dict] = {}

_DEFAULT_ENTRY = {
    "call_id": None,
    "business_name": None,
    "task_description": None,
    "status": "starting",
    "transcript_lines": [],
    "listen_url": None,
}


def start(user_id: int, call_id: str, business_name: str | None, task_description: str) -> None:
    """Called once dial_confirmed_call successfully places the call."""
    _state[user_id] = {
        "call_id": call_id,
        "business_name": business_name,
        "task_description": task_description,
        "status": "dialing",
        "transcript_lines": [],
        "listen_url": None,
    }


def set_status(user_id: int, text: str) -> None:
    """Updates the one-line "what's happening" text the live-listen page
    polls (dialing / ringing / in progress / wrapping up / ended)."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["status"] = text


def add_transcript_line(user_id: int, text: str) -> None:
    """Appends one line to the running (scrubbed, see call_tools.py's
    _scrub_sensitive) transcript log shown on the live-listen page --
    capped so a long call can't grow this unbounded in memory."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["transcript_lines"].append(text)
    if len(entry["transcript_lines"]) > MAX_TRANSCRIPT_LINES:
        entry["transcript_lines"] = entry["transcript_lines"][-MAX_TRANSCRIPT_LINES:]


def set_listen_url(user_id: int, listen_url: str) -> None:
    """Vapi's own listenUrl -- see module docstring for why this is held
    ONLY here, in memory, and never persisted or sent to the browser
    directly. Called once the create-call response (or an early webhook
    event) actually carries it."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["listen_url"] = listen_url


def get(user_id: int) -> dict:
    """Never raises -- gives back a default/empty entry (status='starting',
    no call_id, nothing to show) for a user with nothing currently
    tracked, same "never raises" convention as live_activity.get."""
    entry = _state.get(user_id)
    if entry is None:
        return dict(_DEFAULT_ENTRY, transcript_lines=[])
    return {
        "call_id": entry.get("call_id"),
        "business_name": entry.get("business_name"),
        "task_description": entry.get("task_description"),
        "status": entry.get("status"),
        "transcript_lines": list(entry.get("transcript_lines", [])),
        "listen_url": entry.get("listen_url"),
    }


def clear(user_id: int) -> None:
    """Called once a call fully ends (success, failure, or error) -- so the
    live-listen page never shows a stale in-progress state for a call
    that's already over."""
    _state.pop(user_id, None)
