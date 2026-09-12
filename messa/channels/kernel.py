"""Thin async REST client for Kernel (https://kernel.sh) cloud browser endpoints.

Provides the same high-level interface as messa/channels/browserbase.py so
deepsearch, Stagehand, and Playwright MCP can connect seamlessly over CDP
with zero changes:

- create_session(context_id: str | None) -> dict[str, Any]
- release_session(session_id: str) -> None
- get_live_view_url(session_id: str) -> str | None
- get_session_pages(session_id: str) -> list[dict[str, Any]]

Kernel runs serverless cloud browsers with:
1. Native `stealth: true` anti-detection (Canvas, WebGL, Audio, CDP masking).
2. Unlimited residential proxies included at zero extra bandwidth cost (bypasses
   Walmart's "Press and Hold" PerimeterX / HUMAN Security block).
3. Serverless execution billing per GB-second ($0 for idle pauses).
"""
from __future__ import annotations

from typing import Any

import httpx

from .. import config

BASE_URL = getattr(config, "KERNEL_BASE_URL", None) or "https://api.onkernel.com"


class KernelError(RuntimeError):
    """Raised on a non-2xx response from Kernel, or when KERNEL_API_KEY is missing."""


def _headers() -> dict[str, str]:
    if not config.KERNEL_API_KEY:
        raise KernelError(
            "KERNEL_API_KEY not set in .env / HF Space secrets -- required when BROWSER_PROVIDER='kernel'. "
            "Get a free key with $5/mo credits at https://kernel.sh."
        )
    return {
        "Authorization": f"Bearer {config.KERNEL_API_KEY}",
        "Content-Type": "application/json",
    }


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, f"{BASE_URL}{path}", headers=_headers(), **kwargs)
    if resp.status_code >= 300:
        raise KernelError(f"Kernel {method} {path} failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json() if resp.content else {}


async def create_profile(name: str) -> str:
    """Create a persistent profile in Kernel. Profiles store cookies, localStorage,
    and login state indefinitely across sessions."""
    data = await _request("POST", "/profiles", json={"name": name})
    return data.get("name") or data.get("id") or name


async def create_session(context_id: str | None = None, *, stealth: bool = True) -> dict[str, Any]:
    """Start a Kernel browser session with stealth mode and residential proxies.

    With a context_id (mapped from user_id or profile name), cookies and storage
    persist back to that profile and are restored across runs.

    Returns a standardized dictionary containing:
    - id: the Kernel session ID
    - connectUrl: the WebSocket URL for CDP (handed to @playwright/mcp or Playwright)
    - liveViewUrl: embeddable interactive Live View URL
    """
    body: dict[str, Any] = {
        "stealth": stealth,
    }
    if context_id:
        body["profile"] = {"name": context_id}
        body["profile_save_changes"] = True

    data = await _request("POST", "/browsers", json=body)
    session_id = data.get("session_id") or data.get("id") or ""
    cdp_url = data.get("cdp_ws_url") or data.get("connectUrl") or ""
    live_view = data.get("browser_live_view_url") or data.get("liveViewUrl") or None

    return {
        "id": session_id,
        "connectUrl": cdp_url,
        "liveViewUrl": live_view,
        "raw": data,
    }


async def get_live_view_url(session_id: str) -> str | None:
    """Retrieve the interactive Live View URL for a running Kernel browser session."""
    try:
        data = await _request("GET", f"/browsers/{session_id}")
        return data.get("browser_live_view_url") or data.get("liveViewUrl")
    except Exception:
        return None


async def release_session(session_id: str) -> None:
    """Terminate the Kernel browser session and release its cloud resources."""
    try:
        await _request("DELETE", f"/browsers/{session_id}")
    except Exception:
        pass


async def get_session_pages(session_id: str) -> list[dict[str, Any]]:
    """Inspect open browser tabs for live view tab switching. Best-effort."""
    try:
        data = await _request("GET", f"/browsers/{session_id}")
        pages = data.get("pages") or []
        if not pages and data.get("session_id"):
            return [{"id": "default", "url": data.get("url", ""), "title": "Kernel Browser"}]
        return pages
    except Exception:
        return []
