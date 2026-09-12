"""Unified cloud browser provider router for Messa.

Dynamically routes browser sessions to either Browserbase or Kernel based on
`config.BROWSER_PROVIDER` ("browserbase" or "kernel"):

- "browserbase" (default): Runs sessions via Browserbase (with residential proxy
  routing and CAPTCHA solving to preserve the user's existing subscription).
- "kernel": Runs serverless sessions via Kernel (with built-in stealth anti-detection,
  unlimited residential proxy bandwidth, and persistent profiles).

This abstraction ensures that deepsearch, Stagehand, Playwright MCP, and the
live-view page connect to a uniform CDP interface without provider coupling.
"""
from __future__ import annotations

import logging
from typing import Any

from .. import config
from . import browserbase, kernel

logger = logging.getLogger(__name__)


def get_active_provider() -> str:
    """Return normalized active browser provider ('browserbase' or 'kernel')."""
    provider = (config.BROWSER_PROVIDER or "browserbase").strip().lower()
    return "kernel" if provider == "kernel" else "browserbase"


async def create_session(
    context_id: str | None = None,
    *,
    user_id: int | None = None,
    stealth: bool = True,
) -> dict[str, Any]:
    """Create a new browser session with the active provider.

    Returns a standardized dictionary:
    - id: Session ID string
    - connectUrl: CDP WebSocket URL string
    - liveViewUrl: Live View URL string (or None)
    - provider: "browserbase" or "kernel"
    """
    provider = get_active_provider()
    if provider == "kernel":
        # For Kernel, a profile name can be derived from user_id if context_id is unset
        profile_key = context_id or (f"user_{user_id}" if user_id else None)
        sess = await kernel.create_session(profile_key, stealth=stealth)
        sess["provider"] = "kernel"
        return sess

    # Default to Browserbase
    sess = await browserbase.create_session(context_id)
    sess["provider"] = "browserbase"
    # Ensure liveViewUrl key exists for parity
    if "liveViewUrl" not in sess and sess.get("id"):
        sess["liveViewUrl"] = await browserbase.get_live_view_url(sess["id"])
    return sess


async def get_live_view_url(session_id: str) -> str | None:
    """Get the interactive Live View URL for a session from the active provider."""
    provider = get_active_provider()
    if provider == "kernel":
        return await kernel.get_live_view_url(session_id)
    return await browserbase.get_live_view_url(session_id)


async def release_session(session_id: str) -> None:
    """Release a cloud browser session."""
    provider = get_active_provider()
    if provider == "kernel":
        await kernel.release_session(session_id)
    else:
        await browserbase.release_session(session_id)


async def get_session_pages(session_id: str) -> list[dict[str, Any]]:
    """Return open browser pages/tabs."""
    provider = get_active_provider()
    if provider == "kernel":
        return await kernel.get_session_pages(session_id)
    return await browserbase.get_session_pages(session_id)


def _coerce_int_user_id(user_id: Any | None) -> int | None:
    if isinstance(user_id, int):
        return user_id
    if isinstance(user_id, str) and user_id.isdigit():
        return int(user_id)
    return None


async def _finalize_light_web_run(
    managed: Any,
    agent: Any,
    decision: Any,
    *,
    int_user_id: int | None,
    debug_domain_hint: str | None = None,
) -> dict[str, Any]:
    """Shared tail for both run_light_web_task and resume_light_web_task:
    optional debug screenshot, conditional session release (never release
    while NEEDS_HUMAN -- that's the whole point of suspending instead of
    tearing down), and the standardized result dict both entry points
    return. Factored out so the two entry points can't silently drift
    (e.g. one of them forgetting to release a finished session, or
    resume's result dict missing a field run's callers already rely on).
    """
    from .. import live_activity

    # Optional debug screenshot capture, gated behind an explicit config
    # value rather than hardcoded to any one machine/person's local
    # folder (a prior version of this hardcoded a specific developer's
    # IDE debug directory here -- dead in any real deployment, but
    # leftover scaffolding that had no business being in the main
    # task-runner every real user request goes through).
    debug_dir = getattr(config, "LIGHT_WEB_AGENT_DEBUG_SCREENSHOT_DIR", "") or ""
    if debug_dir and managed.active_page:
        from pathlib import Path
        artifact_dir = Path(debug_dir)
        try:
            artifact_dir.mkdir(parents=True, exist_ok=True)
            hint = debug_domain_hint or ""
            domain_slug = "instacart" if "instacart" in hint else "walmart" if "walmart" in hint else "web"
            await managed.active_page.screenshot(path=str(artifact_dir / f"{domain_slug}_browserbase_live.png"))
        except Exception as ss_e:
            logger.debug(f"[_finalize_light_web_run] screenshot capture skipped: {ss_e}")

    # If completed or failed, release session unless waiting on human
    if decision.status not in ["NEEDS_HUMAN"]:
        if int_user_id is not None:
            try:
                live_activity.set_closing(int_user_id)
            except Exception:
                pass
        await session_manager.release_session(managed.session_id)
        if int_user_id is not None:
            try:
                live_activity.clear(int_user_id)
            except Exception:
                pass

    res = decision.dict()
    res["session_id"] = managed.session_id
    res["live_view_url"] = managed.live_view_url
    res["steps_taken"] = len(agent.step_history)
    res["step_history"] = agent.step_history
    return res


async def run_light_web_task(
    goal: str,
    *,
    initial_url: str | None = None,
    user_id: Any | None = None,
    context_id: str | None = None,
    credentials: dict[str, str] | None = None,
    max_steps: int = 15,
) -> dict[str, Any]:
    """Execute a web navigation task using the light-web-agent engine.

    Automatically handles persistent cloud session reuse, network ad/tracker
    shielding, macro-action batching, and human checkpoints (SMS OTP).
    """
    from playwright.async_api import async_playwright
    from .. import live_activity
    from .browser_session_manager import session_manager
    from .light_web_agent import LightWebAgent

    int_user_id = _coerce_int_user_id(user_id)

    # 1. Get or create persistent session
    managed = await session_manager.get_or_create_session(
        user_id=user_id,
        context_id=context_id,
    )

    if int_user_id is not None:
        try:
            live_activity.start(int_user_id, goal)
            if managed.session_id:
                live_activity.set_session_id(int_user_id, managed.session_id)
        except Exception as la_e:
            logger.debug(f"[run_light_web_task] live_activity start skipped: {la_e}")

    async with async_playwright() as p:
        page = await session_manager.connect_playwright(managed, p)
        if initial_url and page:
            try:
                await page.goto(initial_url, timeout=45000, wait_until="domcontentloaded")
            except Exception as e:
                logger.warning(f"[run_light_web_task] goto warning for {initial_url}: {e}")

        agent = LightWebAgent(
            session=managed,
            user_goal=goal,
            credentials=credentials,
            max_steps=max_steps,
            user_id=user_id,
        )
        decision = await agent.run()
        return await _finalize_light_web_run(
            managed, agent, decision, int_user_id=int_user_id, debug_domain_hint=initial_url,
        )


async def resume_light_web_task(
    user_id: Any,
    human_input: str,
    *,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Resume a light-web-agent task that suspended on a human checkpoint
    (OTP digits texted back, a CAPTCHA confirmation, a risk-review "YES",
    etc), using whatever `db.get_pending_light_web_checkpoint` persisted
    when it suspended (see LightWebAgent._persist_pending_checkpoint).

    The original `run_light_web_task` call's own `async with
    async_playwright()` block already exited by the time it returned
    NEEDS_HUMAN -- that's what let the turn actually end instead of
    blocking a whole asyncio task on a human reply (unlike deepsearch's
    poll-inside-one-long-task model). So this reconnects with a FRESH
    Playwright driver over the same remote cloud session's CDP url rather
    than reusing anything from that exited context.
    """
    from playwright.async_api import async_playwright
    from .. import db, live_activity
    from .browser_session_manager import session_manager
    from .light_web_agent import HumanCheckpoint, LightWebAgent

    int_user_id = _coerce_int_user_id(user_id)
    if int_user_id is None:
        return {"status": "FAILED", "result_summary": "resume_light_web_task requires a real user_id."}

    task = await db.get_pending_light_web_checkpoint(int_user_id)
    if not task:
        return {
            "status": "FAILED",
            "result_summary": "No light-web-agent task is currently waiting on a human checkpoint.",
        }

    artifacts = task.get("artifacts") or {}
    pending = artifacts.get("pending_checkpoint") or {}
    call_context = artifacts.get("_call_context") or {}
    resolved_session_id = session_id or pending.get("session_id")

    managed = session_manager.get_session(resolved_session_id) if resolved_session_id else None
    if not managed:
        # The in-memory session is gone (process restarted, or it was
        # already released/TTL'd out from under this task) -- nothing to
        # reconnect to. Mark the task failed rather than leaving it stuck
        # forever in 'waiting_user_input' with no way to ever resume.
        try:
            await db.set_active_task_status(task["task_id"], "failed")
        except Exception:
            pass
        return {
            "status": "FAILED",
            "result_summary": "The browser session for this task is no longer available (it may have timed out). Please start the task again.",
        }

    resumed = await session_manager.resume_session(managed.session_id, human_input)
    if not resumed:
        return {
            "status": "FAILED",
            "result_summary": "Could not resume the suspended browser session.",
        }

    # Force a fresh CDP reconnect below -- browser_inst/context/active_page
    # on `managed` are stale Python objects bound to the PREVIOUS
    # async_playwright() driver process, which already exited. Leaving
    # them set would make connect_playwright's own "already connected"
    # fast-path (`if session.browser_inst and session.active_page and not
    # ...is_closed()`) short-circuit onto dead objects instead of
    # reconnecting.
    managed.browser_inst = None
    managed.context = None
    managed.active_page = None

    checkpoint = HumanCheckpoint(
        kind=pending.get("kind", "otp"),
        prompt_to_user=pending.get("prompt_to_user", ""),
        trigger_reason="resumed_from_persisted_checkpoint",
        human_input=human_input,
    )
    goal = call_context.get("goal") or ""
    credentials = dict(call_context.get("credentials") or {})
    credentials["checkpoint_answer"] = human_input
    max_steps = call_context.get("max_steps", 15)

    if int_user_id is not None:
        try:
            live_activity.start(int_user_id, goal)
            live_activity.set_session_id(int_user_id, managed.session_id)
        except Exception as la_e:
            logger.debug(f"[resume_light_web_task] live_activity start skipped: {la_e}")

    async with async_playwright() as p:
        await session_manager.connect_playwright(managed, p)

        agent = LightWebAgent(
            session=managed,
            user_goal=goal,
            credentials=credentials,
            max_steps=max_steps,
            user_id=user_id,
            resume_checkpoint=checkpoint,
        )
        agent.active_task_id = task.get("task_id")
        agent.scratchpad_artifacts = artifacts
        decision = await agent.run()
        return await _finalize_light_web_run(managed, agent, decision, int_user_id=int_user_id)



