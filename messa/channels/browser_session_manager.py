"""Persistent Cloud Browser Session & Page Stack Manager for Messa.

Manages persistent cloud browser sessions (Browserbase / Kernel) across multi-turn
conversations, human checkpoints (SMS OTP), and multi-tab / popup workflows.

Features:
- Session-as-State: Cloud browsers stay alive across human turns without dying.
- Keepalive Heartbeat: 40s ping during SUSPENDED_WAITING_INPUT to prevent cloud timeout.
- 5-Minute Hard TTL: Automatically tears down abandoned sessions to prevent zombie billing.
- PageStackManager: Tracks popups (window.open / target="_blank"), auto-switches focus
  for allowlisted origins (Google/Apple SSO, 3D Secure), and flags non-allowlisted popups.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from . import browser

logger = logging.getLogger(__name__)

DEFAULT_ALLOWLISTED_ORIGINS = [
    "accounts.google.com",
    "appleid.apple.com",
    "login.live.com",
    "auth0.com",
    "cardinalcommerce.com",
    "modirum.com",
    "arcot.com",
    "verifiedbyvisa.com",
    "mastercard.com",
]


class PopupOriginPolicy(BaseModel):
    allowlisted_origins: List[str] = Field(default_factory=lambda: list(DEFAULT_ALLOWLISTED_ORIGINS))
    on_non_allowlisted_popup: Literal["risk_review", "block"] = "risk_review"

    def is_allowed(self, target_url: str, parent_url: Optional[str] = None) -> bool:
        """Check whether the popup URL origin is allowlisted or matches parent domain."""
        if not target_url:
            return False
        try:
            parsed = urlparse(target_url)
            host = (parsed.hostname or "").lower()
            if not host:
                return False

            # Allow if host matches allowlist
            for allowed in self.allowlisted_origins:
                allowed_lower = allowed.lower()
                if host == allowed_lower or host.endswith("." + allowed_lower):
                    return True

            # Allow if it shares registrable domain with the parent page (e.g. auth.walmart.com & www.walmart.com)
            if parent_url:
                parent_parsed = urlparse(parent_url)
                parent_host = (parent_parsed.hostname or "").lower()
                if parent_host:
                    if host == parent_host or host.endswith("." + parent_host):
                        return True
                    # Compare base domain (e.g. walmart.com)
                    host_parts = host.split(".")
                    parent_parts = parent_host.split(".")
                    if len(host_parts) >= 2 and len(parent_parts) >= 2:
                        base_host = ".".join(host_parts[-2:])
                        base_parent = ".".join(parent_parts[-2:])
                        if base_host == base_parent:
                            return True
            return False
        except Exception:
            return False


class ManagedBrowserSession:
    """Represents an active, managed cloud browser session."""

    def __init__(
        self,
        session_id: str,
        provider: str,
        cdp_url: str,
        live_view_url: Optional[str] = None,
        user_id: Optional[int] = None,
        context_id: Optional[str] = None,
        origin_policy: Optional[PopupOriginPolicy] = None,
    ) -> None:
        self.session_id = session_id
        self.provider = provider
        self.cdp_url = cdp_url
        self.live_view_url = live_view_url
        self.user_id = user_id
        self.context_id = context_id
        self.origin_policy = origin_policy or PopupOriginPolicy()

        self.playwright: Any = None
        self.browser_inst: Any = None
        self.context: Any = None
        self.active_page: Any = None
        self.page_stack: List[Any] = []

        self.status: Literal["ACTIVE", "SUSPENDED_WAITING_INPUT", "TERMINATED"] = "ACTIVE"
        self.created_at = time.time()
        self.last_activity_at = time.time()
        self.keepalive_task: Optional[asyncio.Task] = None
        self.ttl_task: Optional[asyncio.Task] = None
        self.checkpoint: Optional[Dict[str, Any]] = None
        self._on_untrusted_popup_cb: Optional[Callable[[str], None]] = None

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity_at = time.time()

    async def ping_keepalive(self) -> bool:
        """Send a lightweight keepalive ping to the active page."""
        if not self.active_page:
            return False
        try:
            if not self.active_page.is_closed():
                await self.active_page.evaluate("() => 1 + 1")
                self.touch()
                return True
        except Exception as e:
            logger.debug(f"[ManagedSession {self.session_id}] Keepalive ping failed: {e}")
        return False

    def setup_page_listeners(self) -> None:
        """Attach listeners to context for popups, multi-tabs, and crashes."""
        if not self.context:
            return

        def handle_new_page(new_page: Any) -> None:
            asyncio.create_task(self._on_popup_opened(new_page))

        self.context.on("page", handle_new_page)

    async def _on_popup_opened(self, new_page: Any) -> None:
        """Handle a new tab or window.open popup."""
        self.touch()
        try:
            # Wait briefly for popup URL to settle
            await asyncio.sleep(0.3)
            popup_url = new_page.url or ""
            parent_url = self.active_page.url if self.active_page else ""

            is_allowed = self.origin_policy.is_allowed(popup_url, parent_url)
            logger.info(
                f"[PageStack] Popup opened: {popup_url[:80]} (allowlisted={is_allowed})"
            )

            if is_allowed:
                # Push previous page to stack, make new popup active
                if self.active_page and self.active_page != new_page:
                    self.page_stack.append(self.active_page)
                self.active_page = new_page

                # When popup closes, restore previous page from stack
                def on_popup_close() -> None:
                    logger.info(f"[PageStack] Popup closed. Restoring previous page.")
                    if self.page_stack:
                        self.active_page = self.page_stack.pop()

                new_page.on("close", on_popup_close)
            else:
                logger.warning(
                    f"[PageStack] Non-allowlisted popup origin: {popup_url}. Flagging as untrusted."
                )
                if self._on_untrusted_popup_cb:
                    self._on_untrusted_popup_cb(popup_url)
        except Exception as e:
            logger.error(f"[PageStack] Error handling popup: {e}")


class BrowserSessionManager:
    """Singleton session manager maintaining active cloud browsers."""

    def __init__(self, default_ttl_seconds: int = 300, keepalive_interval: int = 40) -> None:
        self.default_ttl_seconds = default_ttl_seconds
        self.keepalive_interval = keepalive_interval
        self._sessions: Dict[str, ManagedBrowserSession] = {}
        self._user_to_session: Dict[int, str] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_session(
        self,
        *,
        user_id: Optional[int] = None,
        context_id: Optional[str] = None,
        origin_policy: Optional[PopupOriginPolicy] = None,
        stealth: bool = True,
    ) -> ManagedBrowserSession:
        """Return an active existing session for this user/context, or launch a new one."""
        async with self._lock:
            # Check if user has an existing active or suspended session
            if user_id and user_id in self._user_to_session:
                sess_id = self._user_to_session[user_id]
                session = self._sessions.get(sess_id)
                if session and session.status != "TERMINATED":
                    session.touch()
                    return session

            # Create new cloud session via uniform provider router
            raw = await browser.create_session(
                context_id=context_id,
                user_id=user_id,
                stealth=stealth,
            )
            sess_id = raw.get("id") or raw.get("session_id")
            cdp_url = raw.get("connectUrl") or raw.get("cdp_ws_url")
            live_view = raw.get("liveViewUrl")
            provider_name = raw.get("provider", "browserbase")

            managed = ManagedBrowserSession(
                session_id=sess_id,
                provider=provider_name,
                cdp_url=cdp_url,
                live_view_url=live_view,
                user_id=user_id,
                context_id=context_id,
                origin_policy=origin_policy,
            )

            self._sessions[sess_id] = managed
            if user_id:
                self._user_to_session[user_id] = sess_id

            logger.info(
                f"[SessionManager] Created session {sess_id} via {provider_name} for user {user_id}"
            )
            return managed

    def get_session(self, identifier: Any) -> Optional[ManagedBrowserSession]:
        """Return session by session_id or user_id."""
        if identifier in self._sessions:
            return self._sessions[identifier]
        if identifier in self._user_to_session:
            sess_id = self._user_to_session[identifier]
            return self._sessions.get(sess_id)
        return None

    async def connect_playwright(self, session: ManagedBrowserSession, playwright_api: Any) -> Any:
        """Connect Playwright instance to cloud browser over CDP."""
        if session.browser_inst and session.active_page and not session.active_page.is_closed():
            return session.active_page

        session.playwright = playwright_api
        session.browser_inst = await playwright_api.chromium.connect_over_cdp(session.cdp_url)
        contexts = session.browser_inst.contexts
        if contexts:
            session.context = contexts[0]
        else:
            session.context = await session.browser_inst.new_context()

        pages = session.context.pages
        if pages:
            session.active_page = pages[0]
        else:
            session.active_page = await session.context.new_page()

        session.setup_page_listeners()
        session.touch()
        return session.active_page

    async def suspend_session(
        self,
        session_id: str,
        checkpoint: Dict[str, Any],
    ) -> None:
        """Suspend an active session into SUSPENDED_WAITING_INPUT and start keepalive."""
        session = self._sessions.get(session_id)
        if not session or session.status == "TERMINATED":
            return

        session.status = "SUSPENDED_WAITING_INPUT"
        session.checkpoint = checkpoint
        session.touch()

        # Cancel any existing keepalive
        if session.keepalive_task and not session.keepalive_task.done():
            session.keepalive_task.cancel()

        # Start 40s heartbeat ping task
        async def keepalive_worker() -> None:
            try:
                while session.status == "SUSPENDED_WAITING_INPUT":
                    await asyncio.sleep(self.keepalive_interval)
                    if session.status != "SUSPENDED_WAITING_INPUT":
                        break
                    await session.ping_keepalive()
            except asyncio.CancelledError:
                pass

        session.keepalive_task = asyncio.create_task(keepalive_worker())

        # Start 5-minute Hard TTL watchdog
        if session.ttl_task and not session.ttl_task.done():
            session.ttl_task.cancel()

        async def ttl_watchdog() -> None:
            try:
                await asyncio.sleep(self.default_ttl_seconds)
                if session.status == "SUSPENDED_WAITING_INPUT":
                    logger.warning(
                        f"[SessionManager] Session {session_id} exceeded TTL ({self.default_ttl_seconds}s). Auto-terminating."
                    )
                    await self.release_session(session_id)
            except asyncio.CancelledError:
                pass

        session.ttl_task = asyncio.create_task(ttl_watchdog())
        logger.info(f"[SessionManager] Suspended session {session_id} for checkpoint: {checkpoint.get('kind')}")

    async def resume_session(
        self,
        session_id: str,
        human_input: Optional[str] = None,
    ) -> Optional[ManagedBrowserSession]:
        """Resume a suspended session with user-provided input."""
        session = self._sessions.get(session_id)
        if not session or session.status == "TERMINATED":
            return None

        # Stop keepalive & TTL watchdog
        if session.keepalive_task and not session.keepalive_task.done():
            session.keepalive_task.cancel()
            try:
                await session.keepalive_task
            except (asyncio.CancelledError, Exception):
                pass

        if session.ttl_task and not session.ttl_task.done():
            session.ttl_task.cancel()
            try:
                await session.ttl_task
            except (asyncio.CancelledError, Exception):
                pass


        session.status = "ACTIVE"
        session.touch()
        if session.checkpoint:
            session.checkpoint["human_input"] = human_input

        logger.info(f"[SessionManager] Resumed session {session_id} with input: {'[provided]' if human_input else '[none]'}")
        return session

    async def release_session(self, session_id: str) -> None:
        """Release cloud resources and clean up state."""
        session = self._sessions.pop(session_id, None)
        if not session:
            return

        session.status = "TERMINATED"
        if session.user_id and self._user_to_session.get(session.user_id) == session_id:
            del self._user_to_session[session.user_id]

        if session.keepalive_task and not session.keepalive_task.done():
            session.keepalive_task.cancel()
        if session.ttl_task and not session.ttl_task.done():
            session.ttl_task.cancel()

        # Close local browser connection
        try:
            if session.browser_inst:
                await session.browser_inst.close()
        except Exception as e:
            logger.debug(f"[SessionManager] Error closing browser connection: {e}")

        # Release remote cloud provider resources
        try:
            await browser.release_session(session_id)
            logger.info(f"[SessionManager] Released remote cloud session {session_id}")
        except Exception as e:
            logger.warning(f"[SessionManager] Remote release failed for {session_id}: {e}")

    async def cleanup_all(self) -> None:
        """Clean up all active sessions (e.g. on shutdown)."""
        session_ids = list(self._sessions.keys())
        for sid in session_ids:
            await self.release_session(sid)


# Global singleton instance
session_manager = BrowserSessionManager()
