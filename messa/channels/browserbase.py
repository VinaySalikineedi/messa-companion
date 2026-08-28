"""Thin async REST client for the Browserbase endpoints deepsearch needs.

Deepsearch keeps its existing architecture completely -- the same
@playwright/mcp tool set (browser_navigate, browser_click, browser_snapshot,
...) driven by the same LangChain agent and wrapped by the same safety
guards in deepsearch_tools.py. All this module does is get that MCP server
a *remote* browser to drive instead of a local one: create a Browserbase
session, hand its CDP `connectUrl` to `npx @playwright/mcp@latest
--cdp-endpoint <url>`, and the rest of deepsearch is unchanged. No SDK
dependency -- four endpoints doesn't warrant one, and httpx is already used
the same way for Sendblue (see channels/sendblue.py).

Per Browserbase's own current docs: no `BROWSERBASE_PROJECT_ID` is needed
anywhere -- the API key alone resolves the project.
"""
from __future__ import annotations

from typing import Any

import httpx

from .. import config

BASE_URL = "https://api.browserbase.com/v1"


class BrowserbaseError(RuntimeError):
    """Raised on a non-2xx response, or when BROWSERBASE_API_KEY is unset."""


def _headers() -> dict[str, str]:
    if not config.BROWSERBASE_API_KEY:
        raise BrowserbaseError(
            "BROWSERBASE_API_KEY not set in .env / HF Space secrets -- deepsearch has no "
            "local-browser fallback, so this is required for it to work at all."
        )
    return {"X-BB-API-Key": config.BROWSERBASE_API_KEY, "Content-Type": "application/json"}


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(method, f"{BASE_URL}{path}", headers=_headers(), **kwargs)
    if resp.status_code >= 300:
        raise BrowserbaseError(f"Browserbase {method} {path} failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json() if resp.content else {}


async def create_context() -> str:
    """Create a new persistent Context. Contexts live indefinitely on
    Browserbase's side until explicitly deleted -- callers create one per
    user, once, and reuse its id forever (see db.get_browserbase_context_id/
    save_browserbase_context_id)."""
    data = await _request("POST", "/contexts", json={})
    return data["id"]


# Pinned rather than left to Browserbase's own unconfigured default (which
# its docs don't actually commit to a number for) -- live_view_page.py's
# `.video-wrap` sizes itself to this exact aspect ratio, so the embedded
# live-view iframe fills it edge-to-edge with no letterboxing/dead space
# below the browser content. If you change this, update
# live_view_page.py's BROWSER_VIEWPORT_WIDTH/HEIGHT to match.
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800


async def create_session(context_id: str | None) -> dict[str, Any]:
    """Start a browser session. With a context_id, cookies/logins persist
    back to that Context (persist: true) and are restored from it -- the
    direct replacement for the old local --user-data-dir profile directory,
    except this survives an HF Space restart since it isn't on the
    container's disk at all. Returns the full session dict; callers want
    at least `id` and `connectUrl` (the CDP WebSocket URL).

    Pins `browserSettings.viewport` to VIEWPORT_WIDTH x VIEWPORT_HEIGHT --
    see the comment above those constants for why."""
    body: dict[str, Any] = {
        "browserSettings": {"viewport": {"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT}},
    }
    if context_id:
        body["browserSettings"]["context"] = {"id": context_id, "persist": True}
    return await _request("POST", "/sessions", json=body)


async def get_live_view_url(session_id: str) -> str | None:
    """Fullscreen, iframe-embeddable link to watch the session live --
    captured now (see deepsearch_tools.py) as groundwork for Phase 3's
    "click a link, watch the browser live" feature. Best-effort: callers
    should treat a failure here as non-fatal, it's not required for
    deepsearch to function."""
    data = await _request("GET", f"/sessions/{session_id}/debug")
    return data.get("debuggerFullscreenUrl") or data.get("debuggerUrl")


async def get_session_pages(session_id: str) -> list[dict[str, Any]]:
    """The same GET /sessions/{id}/debug call as get_live_view_url, but
    returns its `pages` array instead of the session-level url -- one entry
    per open tab, each with its own `id`/`url`/`title`/`debuggerUrl`/
    `debuggerFullscreenUrl` (see https://docs.browserbase.com/reference/api/
    session-live-urls). Used by server.py's /live/<token>/status route to
    find a SPECIFIC tab's own live-view link (matched by url against
    live_activity's per-tab state) instead of always showing the
    session-level url, which only ever reflects the original default page.
    Not push/live-updating on Browserbase's side -- callers should re-poll
    this after noticing a tab-of-interest change, not cache it. Best-effort:
    callers should treat a failure here as non-fatal (falls back to the
    session-level live_view_url)."""
    data = await _request("GET", f"/sessions/{session_id}/debug")
    pages = data.get("pages")
    return pages if isinstance(pages, list) else []


async def release_session(session_id: str) -> None:
    """End the session early instead of letting it idle out on its own --
    same "don't leave it running when we're done" principle as fully
    closing the local Chromium process used to be. Best-effort: callers
    should swallow BrowserbaseError here, we're already done with the
    browser either way."""
    await _request("POST", f"/sessions/{session_id}", json={"status": "REQUEST_RELEASE"})
