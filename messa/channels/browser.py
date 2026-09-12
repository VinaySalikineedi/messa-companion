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

    int_user_id = None
    if isinstance(user_id, int):
        int_user_id = user_id
    elif isinstance(user_id, str) and user_id.isdigit():
        int_user_id = int(user_id)

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

        # If artifact directory exists, capture screenshot for audit and verification
        from pathlib import Path
        artifact_dir = Path("/Users/robocafedesktop/.gemini/antigravity-ide/brain/0d960b0d-a3f3-42ec-8a1f-9bc67641b3d0")
        if artifact_dir.exists() and managed.active_page:
            try:
                domain_slug = "instacart" if "instacart" in (initial_url or "") else "walmart" if "walmart" in (initial_url or "") else "web"
                await managed.active_page.screenshot(path=str(artifact_dir / f"{domain_slug}_browserbase_live.png"))
            except Exception as ss_e:
                logger.debug(f"[run_light_web_task] screenshot capture skipped: {ss_e}")


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



