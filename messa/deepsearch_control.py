"""In-memory registry of "which asyncio task is currently running deepsearch
for this user", so a later text from the SAME user can cancel it immediately
instead of the only option being a manual HF Space restart.

Why this works at all: server.py's webhook handler (_process_inbound) has no
per-user queue -- two texts in a row from the same user already run as two
independent, concurrent background tasks today (confirmed directly against
server.py before building this). That's what makes an "instant kill" even
possible: while message #1's turn is still blocked inside deepsearch,
message #2's own fresh orchestrator turn can run RIGHT NOW, notice #1 is
still active (see is_active/describe below), and cancel it -- see
agents/registry.py's cancel_active_search tool and _build_system_prompt's
"active search" hint for the model-driven trigger (only when the user
actually asks to stop or clearly implies switching, never a keyword match).

register()/unregister() are called from tools/deepsearch_tools.py's
build_deepsearch_subagent._run, bracketing the whole delegation -- register
right at the top (before the usage-limits check, so even a request that's
about to be gated is still cancellable... though in practice a gated
request returns near-instantly and is never registered long enough to
matter) and unregister in a `finally` so it's never left dangling on any
exit path, cancellation included.

Deliberately NOT keyed by deepsearch_session_id or tied to db.py's
deepsearch_sessions table -- this is purely an in-process handle to the
live asyncio.Task, gone the instant the process restarts, same "nothing
meaningful survives a restart anyway" reasoning as live_activity.py's own
module docstring (the actual browser session doesn't survive a restart
either).

asyncio.current_task() inside _run resolves to the SAME task FastAPI's
BackgroundTasks/uvicorn created for that one webhook request -- deepagents'
subagent delegation is a plain `await` inside the orchestrator's own graph
walk, not a separate spawned task, so cancelling this handle cancels that
whole in-flight message turn (which, while deepsearch is running, is doing
nothing else) -- exactly "stop the search," not a partial/unsafe cancel."""
from __future__ import annotations

import asyncio

_active_tasks: dict[int, asyncio.Task] = {}
# Human-readable "what is it doing" text, kept alongside the task handle so
# registry.py's system-prompt hint can tell the model what would be
# cancelled without importing live_activity itself (avoids a needless
# cross-module coupling for what's a one-line hint).
_active_titles: dict[int, str] = {}


def register(user_id: int, title: str | None = None) -> None:
    """Called once, at the very top of build_deepsearch_subagent._run, before
    anything else that could take real time -- so a "stop" text sent even
    during the very first (possibly slow) planning LLM call can still find
    and cancel this task."""
    task = asyncio.current_task()
    if task is not None:
        _active_tasks[user_id] = task
        _active_titles[user_id] = title or "a search"


def unregister(user_id: int) -> None:
    """Called in _run's outermost `finally`, so this is cleared on every
    exit path -- clean finish, step-limit cutoff, timeout, error, or a
    cancellation triggered by cancel() itself."""
    _active_tasks.pop(user_id, None)
    _active_titles.pop(user_id, None)


def is_active(user_id: int) -> bool:
    task = _active_tasks.get(user_id)
    return task is not None and not task.done()


def describe(user_id: int) -> str | None:
    """The title/description to show the model for its "should I cancel
    this" judgment call, or None if nothing is running."""
    return _active_titles.get(user_id) if is_active(user_id) else None


def cancel(user_id: int) -> bool:
    """Cancels the in-flight deepsearch task for this user, if any. Returns
    True if there was something to cancel, False if nothing was running
    (already finished, or never started) -- both are normal, expected
    outcomes, never an error. The actual cleanup (releasing the Browserbase
    session, clearing live_activity/db state) happens via the existing
    try/finally chain in tools/deepsearch_tools.py's _run and
    BrowserToolProvider.__aexit__, triggered naturally by the
    asyncio.CancelledError this raises inside that task -- nothing here
    touches the browser or the DB directly."""
    task = _active_tasks.get(user_id)
    if task is not None and not task.done():
        task.cancel()
        return True
    return False
