"""In-memory, per-user "what is this phone automation doing right now" log
-- the Open-Source Phone (BYOP) counterpart to call_activity.py, feeding
the phone-specific live-view page (server.py's GET /live/{token}/phone and
/live/{token}/phone/status), PLUS the one thing call_activity.py can't do
(a call runs on Vapi's own infrastructure, so there's no local task to
cancel) but deepsearch_control.py can: hold a handle to the actual
in-process asyncio.Task running AndroidPhoneAgent.run(), so the live-view
page's "Pause / Abort" break-glass button (open-source-phone.md section 5)
can immediately t.cancel() it, not just flip a flag the agent loop might
not check again for a while.

Deliberately NOT persisted to Postgres -- same reasoning as
call_activity.py/live_activity.py: this is scoped to exactly one in-flight
phone task, already has natural clear points (run_phone_task's `finally`
in tools/android_phone_tools.py), and a live ADB/uiautomator2 connection
can't be serialized across a process restart anyway (devices/android.py's
AndroidDeviceManager makes the same call for its own connection state).

Keyed by user_id, matching call_activity.py/call_control.py/
deepsearch_control.py's own convention.
"""
from __future__ import annotations

import asyncio
import time

# user_id -> the live asyncio.Task running run_android_phone_task/
# resume_android_phone_task for that user right now, if any -- separate
# dict from _state below so a task handle is never accidentally included
# in get()'s returned snapshot (which is meant to be JSON-serializable for
# the live-view status route).
_active_tasks: dict[int, asyncio.Task] = {}

_state: dict[int, dict] = {}

_DEFAULT_ENTRY = {
    "device_id": None,
    "goal": None,
    "status": "starting",
    "thought": None,
    "steps_taken": 0,
    "waiting_prompt": None,
    "screenshot_token": None,
    "started_at": None,
}


def register(user_id: int, device_id: int, goal: str) -> None:
    """Called once, at the very top of tools/android_phone_tools.py's
    run_phone_task (both the fresh-task path and server.py's checkpoint-
    resume short-circuit), before anything else that could take real
    time -- so a Pause/Abort tap even during the very first (possibly
    slow) planner call can still find and cancel this task. Captures
    asyncio.current_task() the same way deepsearch_control.register does,
    for the same reason: deepagents subagent delegation and this engine's
    own run() loop are plain `await`s inside the same task FastAPI's
    request handler created, not a separately spawned task, so cancelling
    this handle cancels the whole in-flight turn."""
    task = asyncio.current_task()
    if task is not None:
        _active_tasks[user_id] = task
    _state[user_id] = {
        "device_id": device_id,
        "goal": goal,
        "status": "running",
        "thought": None,
        "steps_taken": 0,
        "waiting_prompt": None,
        "screenshot_token": None,
        "started_at": time.time(),
    }


def set_status(user_id: int, status: str) -> None:
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["status"] = status


def set_progress(user_id: int, thought: str | None, steps_taken: int) -> None:
    """Called after each planner decision in AndroidPhoneAgent.run() --
    what makes the live-view page feel genuinely live (a running "here's
    what I'm doing" line) rather than just a still screenshot that
    happens to refresh."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["thought"] = thought
    entry["steps_taken"] = steps_taken


def set_waiting(user_id: int, prompt: str | None) -> None:
    """Set on a NEEDS_HUMAN pause (milestone confirmation, unexpected
    dialog, needs_help); cleared (None) the moment the task resumes."""
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["waiting_prompt"] = prompt
    entry["status"] = "waiting_user_input" if prompt else entry.get("status", "running")


def set_screenshot_token(user_id: int, token: str | None) -> None:
    entry = _state.setdefault(user_id, dict(_DEFAULT_ENTRY))
    entry["screenshot_token"] = token


def get(user_id: int) -> dict:
    """Never raises -- gives back a default/empty entry for a user with
    nothing currently tracked, same convention as call_activity.get."""
    entry = _state.get(user_id)
    if entry is None:
        return dict(_DEFAULT_ENTRY)
    return dict(entry)


def has_entry(user_id: int) -> bool:
    """True whenever ANY state is tracked for this user -- a live task
    (is_active), a paused NEEDS_HUMAN checkpoint waiting on the user (no
    live task, but real state worth polling for), or a just-finished
    task's terminal status that hasn't been clear()ed yet. This is the
    right check for the live-view page's /status route (server.py's
    phone_live_view_status): 'active' there means 'there's something to
    show', not narrowly 'a task is currently executing'."""
    return user_id in _state


def is_active(user_id: int) -> bool:
    """True while a real in-process task is still running for this user --
    NOT merely "an entry exists" (an entry lingers briefly after
    unregister races with a poll in a pathological case, and a NEEDS_HUMAN
    pause has no live task at all between suspend and resume, yet still
    has state worth showing -- see get() above for that case)."""
    task = _active_tasks.get(user_id)
    return task is not None and not task.done()


def cancel(user_id: int) -> bool:
    """The break-glass Pause/Abort action's cancellation half (see
    server.py's phone_live_view_abort route for the other half -- pressing
    the Android Home key and releasing the device queue lock). Returns
    True if a live task was actually cancelled, False if nothing was
    running (the caller still presses Home / releases the lock either
    way, since a device can be stuck 'busy' with no live task to show for
    it after a crash)."""
    task = _active_tasks.get(user_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def unregister(user_id: int) -> None:
    """Called in run_phone_task's outermost `finally` (both the fresh-task
    and resume paths) -- cleared on every exit: clean finish, step/timeout
    cutoff, error, or a cancellation triggered by cancel() itself. Only
    drops the task handle; the status snapshot is left in `_state` (with
    status flipped to whatever the caller last set, e.g. 'done'/'failed'/
    'aborted') so the live-view page's last poll shows a real end state
    instead of suddenly reverting to the default 'nothing tracked' entry."""
    _active_tasks.pop(user_id, None)


def clear(user_id: int) -> None:
    """Drops state entirely -- called once the live-view page's own
    'ended' state has had a chance to be polled at least once, or when a
    brand new task starts (register() already overwrites _state, so this
    is mainly for an explicit reset, e.g. in tests)."""
    _active_tasks.pop(user_id, None)
    _state.pop(user_id, None)


def clear_all_for_test() -> None:
    """Test-only reset -- pure process-global state, same as
    call_control.clear_all_for_test / deepsearch_control's own tests."""
    _active_tasks.clear()
    _state.clear()
