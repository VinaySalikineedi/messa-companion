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

from typing import Any

from .. import config
from . import browserbase, kernel


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
