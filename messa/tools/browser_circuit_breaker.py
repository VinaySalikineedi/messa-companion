"""Zero-delta circuit breaker for the Stagehand deepsearch engine
(docs/smart_autonomous_agent_architecture.md's "Dumb Donkey" bug, System 6).

The bug this fixes: during live testing, deepsearch hit a site's anti-fraud
refusal modal ("Account cannot be created right now") and did not recognize
it as terminal -- it kept calling `browser_act` against the same dead page,
spinning up 6 separate cloud browser sessions and repeating the identical
flow 10+ times before a human stopped it. The only failure-tracking that
existed anywhere in this codebase before this module was the LEGACY
Playwright-MCP engine's `consecutive_errors` counter (deepsearch_tools.py),
which only counts THROWN EXCEPTIONS -- a `browser_act` that "succeeds" with
no exception against a refusal modal is invisible to it. The (now-default)
Stagehand engine had no general breaker of any kind before this.

Design: a LangChain `AgentMiddleware` that fingerprints the page's
(url, title) before and after every `browser_act` / `browser_navigate` /
`browser_execute_script` call. An IDENTICAL fingerprint before and after
means the action produced no observable change -- exactly the anti-fraud
-modal case (the click "succeeds" but the page never moves) as well as the
OTP-screen-never-advances case. N consecutive zero-delta actions (default
3, config.DEEPSEARCH_ZERO_DELTA_MAX_REPEATS) raises `ZeroDeltaExceeded`,
which is deliberately NOT caught here -- it propagates up through
`create_agent(...).ainvoke(...)` to deepsearch_tools.py's `_run`, where the
existing generic `except Exception as e:` handler already ends the run
cleanly, saves progress, and reports honestly to the user (the same
"never a silent crash" path _FatalBrowserSessionError/
_TooManyConsecutiveToolErrors already use for the legacy engine).

Why (url, title) and not a full DOM/accessibility-tree diff: a full
snapshot diff is exactly what the LEGACY Playwright-MCP engine already
pays for on every turn (30,000-line accessibility trees per that module's
own docstring) -- it's a big part of why Stagehand replaced it. Re-paying
that cost here, on every single actuation, would undo the latency/cost
win Stagehand exists for. (url, title) is two RPCs already made elsewhere
in stagehand_tools.py (`_get_url`, `page.title()`), so this costs nothing
new. This is a deliberate scope-down, not an oversight: if production
traces ever show a same-title/same-url-but-DOM-changed case slipping
through (a refusal banner injected without changing <title>), the cheap
escalation is adding a lightweight `await page.evaluate("document.body.innerText.length")`
to the fingerprint tuple below -- NOT a full DOM walk.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langgraph.types import Command
from langchain_core.messages import ToolMessage

from .. import config, console

# Tool names this breaker fingerprints. browser_execute_script (added by the
# same reliability-hardening pass this module is part of) is included
# deliberately: a script that silently no-ops (wrong selector, page not
# ready) is exactly the same failure class as a no-op browser_act -- it must
# not get a free pass just because it's raw JS instead of a macro action.
FINGERPRINTED_TOOL_NAMES = frozenset({"browser_act", "browser_navigate", "browser_execute_script"})


class ZeroDeltaExceeded(RuntimeError):
    """Raised when a fingerprinted tool call produces zero observable state
    change (identical page title + URL before and after) too many times in
    a row. This is deliberately not a silent no-op or a soft nudge -- a
    repeated zero-delta action is very likely a terminal block (a refusal
    modal, a dead OTP screen, a stuck CAPTCHA), and the doc's own incident
    showed that letting the model "keep trying" against one burns real
    Browserbase session time across many retries/sessions for zero
    progress. See this module's own docstring for the full design."""


class StagehandZeroDeltaMiddleware(AgentMiddleware):
    """One instance per deepsearch delegation (constructed fresh inside
    deepsearch_tools.py's `_run`, exactly like `_summarization` already is)
    -- the streak counter below has no reason to survive past one
    delegation, so it lives as a plain instance attribute, not a DB row or
    module-level global."""

    def __init__(self, provider: Any, max_zero_delta_repeats: int | None = None) -> None:
        self._provider = provider
        self._max_repeats = (
            max_zero_delta_repeats
            if max_zero_delta_repeats is not None
            else config.DEEPSEARCH_ZERO_DELTA_MAX_REPEATS
        )
        self._streak = 0

    async def _fingerprint(self) -> tuple[str, str]:
        """Never raises -- a fingerprinting hiccup (e.g. no page open yet on
        the very first call) must never be why a real tool call fails; it
        just means this fingerprint compares unequal to any real page
        state later, which is the safe default (never a false zero-delta
        trip from a fingerprinting error)."""
        try:
            page = await self._provider.get_active_page()
        except Exception:
            return ("", "")
        try:
            url = await self._provider._get_url(page)
        except Exception:
            url = ""
        try:
            title = await page.title()
        except Exception:
            title = ""
        return (url, title)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        name = request.tool_call.get("name")
        if name not in FINGERPRINTED_TOOL_NAMES:
            return await handler(request)

        before = await self._fingerprint()
        result = await handler(request)
        after = await self._fingerprint()

        if before == after:
            self._streak += 1
        else:
            self._streak = 0

        if self._streak >= self._max_repeats:
            console.system(
                f"Deepsearch (Stagehand v4): zero-delta circuit breaker tripped -- "
                f"'{name}' produced no observable change {self._streak} times in a row "
                f"(page stuck at {after!r}). Ending run rather than continuing to burn "
                "browser session time against what looks like a terminal block."
            )
            raise ZeroDeltaExceeded(
                f"'{name}' produced zero observable change (same page title/URL) "
                f"{self._streak} times in a row. This is very likely a terminal "
                "block (an error/refusal modal, a dead end, a stuck screen) rather "
                "than a transient glitch -- stop retrying the same approach."
            )

        return result
