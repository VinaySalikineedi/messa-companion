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
(url, title, form_signature) before and after every `browser_act` /
`browser_navigate` / `browser_execute_script` call. An IDENTICAL
fingerprint before and after means the action produced no observable
change -- exactly the anti-fraud-modal case (the click "succeeds" but the
page never moves) as well as the OTP-screen-never-advances case. N
consecutive zero-delta actions (default 3,
config.DEEPSEARCH_ZERO_DELTA_MAX_REPEATS) raises `ZeroDeltaExceeded`,
which is deliberately NOT caught here -- it propagates up through
`create_agent(...).ainvoke(...)` to deepsearch_tools.py's `_run`, where the
existing generic `except Exception as e:` handler already ends the run
cleanly, saves progress, and reports honestly to the user (the same
"never a silent crash" path _FatalBrowserSessionError/
_TooManyConsecutiveToolErrors already use for the legacy engine).

Why (url, title) ALONE was wrong (agent-feedback.md item 1): on any
single-page app (Uber, Amazon checkout, Stripe), filling in a sequence of
form fields -- First Name, then Last Name, then Email -- never changes
the URL or the <title>. Three such `browser_act` calls in a row looked
IDENTICAL to this breaker and tripped `ZeroDeltaExceeded` on the third
field, crashing a perfectly valid task. The team's own suggested fix was
to add `document.body.innerText.length` to the fingerprint -- verified
against actual DOM semantics before implementing it, and it does NOT
work: typing into an `<input>`/`<textarea>` sets that element's `.value`,
which is never part of `document.body.innerText` (innerText reflects only
rendered text NODES, not form control values) -- so `innerText.length`
would have stayed exactly as flat as the URL/title on the very form-fill
case this is meant to fix. Instead, the third fingerprint element below is
`form_signature`: a single `page.evaluate()` call that walks every
input/textarea/select on the page and sums a privacy-safe SIZE signal
(character count for text fields, 0/1 for checkboxes/radios) rather than
concatenating raw values -- typing into a field changes this signature
immediately, while a raw value never leaks into the tuple that gets
logged via `console.system` when the breaker trips.

Why (url, title, form_signature) and not a full DOM/accessibility-tree
diff: a full snapshot diff is exactly what the LEGACY Playwright-MCP
engine already pays for on every turn (30,000-line accessibility trees
per that module's own docstring) -- it's a big part of why Stagehand
replaced it. Re-paying that cost here, on every single actuation, would
undo the latency/cost win Stagehand exists for. `url` and `title` are two
RPCs already made elsewhere in stagehand_tools.py (`_get_url`,
`page.title()`); `form_signature` is one more small, targeted
`page.evaluate()` call (capped to the first 300 form elements) rather than
a full-page walk. This is a deliberate scope-down, not an oversight: if
production traces ever show a same-fingerprint-but-really-changed case
slipping through (e.g. a non-form UI update with no form elements on the
page at all), the next cheap escalation is adding overall
`document.body.innerText.length` alongside `form_signature` -- still not a
full DOM walk -- since that DOES catch prose/content changes, just not
form input values.
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

# Computes a compact, privacy-safe SIZE signal for every form control on the
# page -- never raw values (which would otherwise leak into the fingerprint
# tuple that gets logged via console.system when the breaker trips, and
# could include passwords/PII). Text-like fields contribute their value's
# character COUNT; checkboxes/radios contribute 0/1 for unchecked/checked.
# Capped at the first 300 elements so a single pathological page can't turn
# this into an expensive walk. Wrapped in try/catch inside the JS itself
# (in addition to Python-side error handling in `_fingerprint` below) so a
# single element throwing (a weird custom element, a detached node) can
# never fail the whole evaluate call.
_FORM_SIGNATURE_SCRIPT = (
    "(() => {"
    "  try {"
    "    const els = Array.from(document.querySelectorAll('input, textarea, select')).slice(0, 300);"
    "    let total = 0;"
    "    for (const el of els) {"
    "      try {"
    "        if (el.type === 'checkbox' || el.type === 'radio') {"
    "          total += el.checked ? 1 : 0;"
    "        } else {"
    "          total += (el.value || '').length;"
    "        }"
    "      } catch (e) {}"
    "    }"
    "    return els.length + ':' + total;"
    "  } catch (e) {"
    "    return 'err';"
    "  }"
    "})()"
)


class ZeroDeltaExceeded(RuntimeError):
    """Raised when a fingerprinted tool call produces zero observable state
    change (identical page title + URL + form-field contents before and
    after) too many times in a row. This is deliberately not a silent
    no-op or a soft nudge -- a
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

    async def _fingerprint(self) -> tuple[str, str, str]:
        """Never raises -- a fingerprinting hiccup (e.g. no page open yet on
        the very first call) must never be why a real tool call fails; it
        just means this fingerprint compares unequal to any real page
        state later, which is the safe default (never a false zero-delta
        trip from a fingerprinting error)."""
        try:
            page = await self._provider.get_active_page()
        except Exception:
            return ("", "", "")
        try:
            url = await self._provider._get_url(page)
        except Exception:
            url = ""
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            form_signature = str(await page.evaluate(_FORM_SIGNATURE_SCRIPT))
        except Exception:
            form_signature = ""
        return (url, title, form_signature)

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
                f"'{name}' produced zero observable change (same page title/URL and no "
                f"form field values changed) {self._streak} times in a row. This is very "
                "likely a terminal block (an error/refusal modal, a dead end, a stuck "
                "screen) rather than a transient glitch -- stop retrying the same approach."
            )

        return result
