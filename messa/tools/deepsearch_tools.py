"""Deepsearch subagent (formerly called "browser_agent"): Playwright MCP,
guarded, opened fresh per task and fully closed afterward -- with progress
saved to the DB so a run cut off by its step limit can be resumed instead
of starting over.

Safety layer ported from the original `agent2.py` browser agent essentially
unchanged:

  * domain allow-list on navigation,
  * destructive actions (typing, clicking, uploads, raw JS eval, dialogs)
    require human confirmation via the shared ApprovalGate,
  * element refs must come from a *fresh* snapshot -- if the page navigated
    since the last snapshot, click/type/hover/drag/select are refused until
    a new snapshot is taken, since stale refs silently point at the wrong
    element.

Browser location: the actual Chromium no longer runs inside our own
container -- two rounds of "Chrome isn't installed" failures on the HF
Space (see config.py's Browserbase comment) came from exactly that, so the
browser now lives on Browserbase's infrastructure instead, and
`@playwright/mcp` connects to it remotely over CDP (`--cdp-endpoint
<connectUrl>`, see channels/browserbase.py) rather than launching a local
process. Everything downstream of that -- the tool set, the LangChain
agent, the guard layer below -- is completely unchanged; only *where* the
browser physically runs is different.

Lifecycle: `build_deepsearch_subagent()` returns a deepagents
`CompiledSubAgent` whose runnable connects `@playwright/mcp` fresh to a new
Browserbase session on each delegation and fully closes/releases it once
the subagent's task finishes -- so an idle Messa session isn't holding a
browser session open (and racking up Browserbase usage). Logins still
survive between tasks because each user gets a persistent Browserbase
Context (created once, reused forever -- see db.get_browserbase_context_id)
rather than an unpersisted one-off session.

Multi-site delegation: the top-level BrowserToolProvider owns one Browserbase
session and runs `@playwright/mcp` as a local HTTP server against it with
`--shared-browser-context` (rather than the single stdio connection used
before) -- this lets the `delegate_website_task` tool open brand-new,
independent MCP client connections to that SAME server, each claiming its
own tab within the one Browserbase session, instead of opening (and paying
for) a separate Browserbase session per site.

Tab isolation is achieved WITHOUT `--isolated` (an earlier version of this
used that flag; removed -- see below). `--shared-browser-context` alone
does NOT stop two connections from colliding on the very same page once a
real `--cdp-endpoint` (an externally-owned browser, exactly how a
Browserbase session is connected -- not a browser `@playwright/mcp` launches
itself) is involved: with neither flag, and neither connection explicitly
claiming a tab, both connections implicitly share the server's one "current
page" pointer, so one connection's `browser_navigate` can be "interrupted by
another navigation" from the other (confirmed the hard way, see
/tmp/test_multisite_delegation_live.py's history). `--isolated` used to fix
that by giving each connection its own separate browser CONTEXT -- but that
also moved every sub-worker's real activity out of Browserbase's default
context, which is the one thing its live-view/debug-URL tracking actually
follows (Browserbase's own docs: "Always use the default context and page
when possible to ensure proper functionality of Verified features") --
hence the live view showing a permanently blank default page while
sub-workers were doing real, logged work on tabs Browserbase's tracking
never saw. The fix: every sub-worker calls `@playwright/mcp`'s own
`browser_tabs(action="new")` tool as the FIRST thing it does on its fresh
connection (see __aenter__ below), claiming its own tab WITHIN the shared
default context instead of a separate one -- confirmed empirically (see
/tmp/probe_tabs_new.py) that this gives each connection the same isolation
`--isolated` did (zero cross-talk, zero navigation-interruption errors)
while everything stays inside the one context Browserbase actually tracks
end-to-end, including per-tab live-view URLs (channels/browserbase.
get_session_pages). The top-level provider needs no such call -- it never
shares its connection with anyone, so it simply keeps using Browserbase's
original default page, exactly as before multi-site delegation existed.

A model that calls `delegate_website_task` several times in one turn gets
real concurrency for free from LangGraph's own tool-calling loop (see
/tmp/test_agent_concurrency.py), bounded by
`config.DEEPSEARCH_MAX_SUBAGENTS` via a shared `asyncio.Semaphore`. Each
sub-worker is a full `BrowserToolProvider` in its own right (same guard
layer, same `request_human_help`), just constructed with `server_url` set
instead of creating its own session/process -- see that parameter's
docstring on `__init__`. Each sub-worker is deliberately scoped small and
bounded -- one site, one goal, a short step budget
(config.DEEPSEARCH_SUBAGENT_MAX_STEPS) -- mirroring what deepsearch used to
do sequentially per site before this feature existed, not a new, more
open-ended kind of task; see _SUBAGENT_SYSTEM_PROMPT below.

Resumability: the `task` tool's schema only carries free-text `description`
+ `subagent_type` (deepagents fixes this; there's no side channel for
structured args), so Messa references a prior run by writing "session
#<id>" in her delegation description -- see `_SESSION_REF_RE` below. The
subagent looks that up, and if found, replays the session's saved message
history (including prior tool calls/results) before continuing, instead of
starting the LangGraph run from scratch. Every run persists its resulting
message history back to the DB regardless of whether it finished cleanly
or got cut off by `DEEPSEARCH_MAX_STEPS` (detected via `GraphRecursionError`
+ an in-call `MemorySaver` checkpointer, which is what lets us recover the
partial message list even though `.ainvoke()` itself doesn't return one on
a raised exception) -- so the next delegation can always pick up from
exactly where this one left off.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_from_dict, messages_to_dict
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

from .. import config, console, credentials, db, deepsearch_control, live_activity, reliability, usage
from ..approval import ApprovalGate
from ..channels import browserbase, sendblue
from ..channels.browserbase import BrowserbaseError
from ..channels.sendblue import SendblueError
from ..config import UserContext
from .browser_circuit_breaker import StagehandZeroDeltaMiddleware
from .scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block
from .search_engine import unified_web_read as _unified_web_read
from .search_engine import unified_web_search as _unified_web_search
from .stagehand_tools import STAGEHAND_SYSTEM_PROMPT, StagehandToolProvider

LABEL = "deepsearch"

DESTRUCTIVE_TOOLS = {
    "browser_press_key",
    "browser_drag",
    "browser_file_upload",
    "browser_evaluate",
    "browser_run_code_unsafe",
    "browser_handle_dialog",
}
SNAPSHOT_DEPENDENT_TOOLS = {
    "browser_click", "browser_type", "browser_hover", "browser_drag", "browser_select_option",
}

class _FatalBrowserSessionError(RuntimeError):
    """Raised (never swallowed into a retryable error string) when a tool
    call fails because the WHOLE Browserbase session is confirmed gone --
    either it died mid-run (410/"Gone"/"session not running", see
    _is_dead_browser_session_error) or it could never be opened in the
    first place (Browserbase down/quota/auth failure inside
    BrowserToolProvider._ensure_live_session). Every other tab sharing this
    session (the top-level tab and every delegate_website_task sub-worker
    alike) is equally doomed, so this is deliberately let propagate all the
    way out of the run instead of being retried -- see guarded()'s except
    block for where it's raised and _delegate_website_task's own comment
    for why it's re-raised through both nesting levels there, unlike
    _TooManyConsecutiveToolErrors below."""


class _TooManyConsecutiveToolErrors(RuntimeError):
    """Raised when one tab (top-level or a single sub-worker) has failed
    config.DEEPSEARCH_MAX_CONSECUTIVE_TOOL_ERRORS tool calls in a row, for
    ANY reason -- a broader, softer backstop than
    _FatalBrowserSessionError's exact-signature match, for a run that's
    clearly stuck without necessarily being a dead-session case. Unlike
    _FatalBrowserSessionError, this is deliberately NOT re-raised past
    _delegate_website_task's own error handling -- a single sub-worker
    tab being stuck (a page that keeps rejecting a selector, say) says
    nothing about whether OTHER sites/tabs are still workable, so it's
    left to fall through to the existing generic "(errored: ...)" handling
    there, reported back to the top-level model as one failed site among
    however many it delegated -- exactly like any other sub-worker
    failure already was, just capped now instead of open-ended."""


# In-memory registry of live BrowserToolProvider instances kept warm across
# resumed turns / steps. When a task pauses (due to step budget or human-in-the-loop wait),
# keeping the provider alive in memory preserves the remote Browserbase session, the
# @playwright/mcp Node process, open modals, and form inputs -- eliminating the latency
# and state loss of reconnecting to about:blank on resume.
_WARM_SESSION_PROVIDERS: dict[int, Any] = {}
_WARM_SESSION_EXPIRY: dict[int, float] = {}
WARM_SESSION_TTL_SECONDS = 600.0  # 10 minutes session warmth TTL


async def _cleanup_expired_warm_sessions() -> None:
    now = time.monotonic()
    expired = [sid for sid, exp in _WARM_SESSION_EXPIRY.items() if now > exp]
    for sid in expired:
        _WARM_SESSION_EXPIRY.pop(sid, None)
        prov = _WARM_SESSION_PROVIDERS.pop(sid, None)
        if prov is not None:
            console.system(f"Deepsearch: warm session #{sid} expired from memory cache -- closing.")
            try:
                prov._is_warm = False
                await prov.close()
            except Exception as e:  # noqa: BLE001
                console.system(f"Deepsearch: error closing expired warm session #{sid}: {e}")


async def purge_warm_sessions_for_user(user_id: int) -> list[int]:
    """Forcefully closes and evicts all warm browser sessions belonging to this user.
    Used by the kill switch to immediately terminate Browserbase billing and MCP
    processes when the user asks to stop or cancel."""
    purged: list[int] = []
    for sid, prov in list(_WARM_SESSION_PROVIDERS.items()):
        if getattr(prov, "_user_id", None) == user_id:
            _WARM_SESSION_PROVIDERS.pop(sid, None)
            _WARM_SESSION_EXPIRY.pop(sid, None)
            if prov is not None:
                console.system(f"Deepsearch: purging warm browser session #{sid} for user #{user_id}.")
                try:
                    prov._is_warm = False
                    await prov.close()
                    purged.append(sid)
                except Exception as e:  # noqa: BLE001
                    console.system(f"Deepsearch: error closing purged session #{sid}: {e}")
    return purged


def count_warm_sessions_for_user(user_id: int) -> int:
    """Returns the count of active warm, in-memory browser sessions held for this user."""
    return sum(
        1 for sid, prov in _WARM_SESSION_PROVIDERS.items()
        if getattr(prov, "_user_id", None) == user_id
    )


_DEAD_SESSION_ERROR_MARKERS = ("410", "session not running")


def _is_dead_browser_session_error(e: Exception) -> bool:
    """True for the exact error shape a confirmed-gone Browserbase session
    produces (see error.txt's own production trace: 'WebSocket error: ...
    410 Gone - session not running', and the matching 'Browserbase GET
    /sessions/.../debug failed (410): ... "Session stopped"' from the
    separate live-view polling path) -- checked as a plain substring match
    on str(e) rather than trying to parse a specific exception type, since
    the 410 can surface through several different layers (the MCP client's
    own WebSocket connect, a raw Browserbase API call) each with their own
    exception shape. Deliberately narrow (both markers required, not just
    "410" alone, which could coincidentally appear in an unrelated error
    string) -- a false negative here just falls through to the softer
    _TooManyConsecutiveToolErrors backstop instead, never a crash; a false
    positive would end a run that might have kept working, which is the
    worse mistake, so precision matters more than recall here."""
    text = str(e)
    return all(marker in text for marker in _DEAD_SESSION_ERROR_MARKERS)


# Process-wide cache of @playwright/mcp's tool schemas -- (name,
# description, args_schema) triples, discovered once for real (the first
# deepsearch run in this process's lifetime) and reused by every later
# top-level BrowserToolProvider so it can hand the model a complete,
# correctly-typed toolset from turn one WITHOUT opening a real (billed)
# Browserbase session just to learn what the tools are -- see
# BrowserToolProvider._ensure_live_session's own docstring for the full
# lazy-open design this enables. Schemas are static per @playwright/mcp
# version, not per-session, so this is safe to reuse across every user and
# every run for the life of the process; a fresh process (restart/deploy)
# just repopulates it on its own first run. None until that happens.
_CACHED_TOOL_SPECS: list[tuple[str, str, Any]] | None = None

_SESSION_REF_RE = re.compile(r"session\s*#?\s*(\d+)", re.IGNORECASE)

# Process-wide cap on TOP-LEVEL deepsearch sessions (distinct from
# BrowserToolProvider._subagent_semaphore above, which bounds sub-worker
# tabs WITHIN one already-running session). Each top-level session spawns
# its own @playwright/mcp Node subprocess and holds one Browserbase
# session for the run's whole duration -- both real, per-session costs
# that don't shrink just because the box is under load. Without a cap, a
# burst of simultaneous "deepsearch" delegations (easy at launch-scale
# traffic) can spawn unbounded Node subprocesses on the one box this app
# runs on (see config.py's DEEPSEARCH_MAX_CONCURRENT_SESSIONS comment for
# the full reasoning) and/or exceed the real Browserbase plan's concurrent-
# session limit, which fails BOTH the request that pushed it over AND
# whichever other concurrent session Browserbase decides to reject.
# Created lazily (not at import time) so it's always built against
# whatever config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS is at first use --
# matters for tests, which patch config before the module's own import-time
# state would otherwise have frozen it in.
_session_semaphore: asyncio.Semaphore | None = None


def _get_session_semaphore() -> asyncio.Semaphore:
    global _session_semaphore
    if _session_semaphore is None:
        _session_semaphore = asyncio.Semaphore(config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS)
    return _session_semaphore

# @playwright/mcp's own snapshot refs (from browser_snapshot's output, e.g.
# "[ref=e12]") are always exactly this "e<number>" shape -- verified
# empirically against the real, currently-pinned package version (see
# README's "Deepsearch speed" section) by running it headless in this
# sandbox and inspecting a live snapshot's ref format directly, not
# assumed from documentation. browser_click/type/hover/select_option/drag
# all accept a `target` that's EITHER one of these opaque refs OR "a unique
# element selector" (role=.../text=.../css.../#id) that Playwright resolves
# fresh on every call -- a selector carries none of the staleness risk a
# ref does (the whole reason SNAPSHOT_DEPENDENT_TOOLS below requires a
# fresh browser_snapshot first), so only an actual ref-shaped target needs
# one.
_SNAPSHOT_REF_RE = re.compile(r"^e\d+$")

# Cursor visualization on the live view (per explicit user request, after
# evaluating ghost-cursor/"Playwright-cursor" -- see README's "Cursor
# visualization" section for why those need a raw Playwright Page object
# we don't have, and why this cheaper, in-page approach was chosen
# instead). --init-script (a real, confirmed @playwright/mcp flag) injects
# messa/assets/cursor_overlay.js into every page before any of its own
# scripts run, which defines window.__messaCursor.moveTo(x, y) -- an SVG
# arrow that CSS-transitions to a point and resolves once the transition
# finishes. BrowserToolProvider._move_cursor_to below calls that, via a
# raw (non-model-facing, non-approval-gated) browser_evaluate call, for
# whichever element a click/type/hover/select_option is about to target --
# using the exact SAME target the real action will use next, so the arrow
# genuinely goes where the click will land rather than somewhere
# approximate.
_CURSOR_OVERLAY_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "assets" / "cursor_overlay.js"
CURSOR_ANIMATED_TOOLS = {"browser_click", "browser_type", "browser_hover", "browser_select_option"}

# "Faster Than Human" branch (faster-than-human.md, Upgrade 4): cookie/
# consent banner auto-dismiss, injected the same --init-script way as
# cursor_overlay.js above, independently toggled by
# config.DEEPSEARCH_AUTO_DISMISS_CONSENT -- see that constant's own comment
# and assets/deepsearch_enhancements.js's module docstring for the full
# design and why it's deliberately conservative about what it'll click.
_CONSENT_DISMISS_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "assets" / "deepsearch_enhancements.js"
_CURSOR_MOVE_FN = (
    "(element) => {"
    "const rect = element.getBoundingClientRect();"
    "const x = rect.left + rect.width / 2;"
    "const y = rect.top + rect.height / 2;"
    "return window.__messaCursor ? window.__messaCursor.moveTo(x, y) : undefined;"
    "}"
)

_CURSOR_MOVE_CLICK_FN = (
    "(element) => {"
    "const rect = element.getBoundingClientRect();"
    "const x = rect.left + rect.width / 2;"
    "const y = rect.top + rect.height / 2;"
    "if (window.__messaCursor) window.__messaCursor.moveTo(x, y);"
    "if (window.__messaEffects) window.__messaEffects.ripple(x, y);"
    "}"
)
_CURSOR_MOVE_TYPE_FN = (
    "(element) => {"
    "const rect = element.getBoundingClientRect();"
    "const x = rect.left + rect.width / 2;"
    "const y = rect.top + rect.height / 2;"
    "if (window.__messaCursor) window.__messaCursor.moveTo(x, y);"
    "if (window.__messaEffects) window.__messaEffects.highlightTyping(element);"
    "}"
)

# "Reading" scroll animation on the live view (per explicit user request --
# see config.DEEPSEARCH_READING_ANIMATION's comment and README's "Reading
# animation" section for the full design rationale, including the
# human-centers-content question). Shares cursor_overlay.js's injection
# (same --init-script file defines window.__messaReader alongside
# window.__messaCursor). Fired as a background asyncio task the instant a
# browser_snapshot succeeds (BrowserToolProvider._start_reading_animation --
# NOT awaited inline, so it costs zero latency on the real agent loop) and
# told to stop the instant any subsequent tool call arrives
# (_stop_reading_animation, called at the top of every guarded() call).
# Confirmed empirically (see /tmp/test_mcp_concurrency.py and
# /tmp/test_reading_animation_live.py from the working session that built
# this) that @playwright/mcp pipelines concurrent tool calls over its stdio
# transport rather than queueing one behind the other, so the long-running
# background browser_evaluate("...__messaReader.start()...") call never
# delays the real action's own MCP round trip, and a short, separately
# dispatched "...stop()..." call returns quickly even while that background
# call is still in flight.
_READING_ANIMATION_START_FN = "() => window.__messaReader ? window.__messaReader.start(6) : undefined"
_READING_ANIMATION_STOP_FN = "() => { if (window.__messaReader) window.__messaReader.stop(); }"

# "Faster Than Human" branch (faster-than-human.md, Upgrade 5): a targeted,
# cheap DOM scan for common form-validation-error markers, used by
# check_form_errors below instead of making the model spot an error message
# by eye inside a full browser_snapshot. Same JSON.stringify + raw
# browser_evaluate + _extract_eval_result_json pattern already proven by
# _SET_OF_MARKS_FN/_browser_get_interactive_elements just below -- this is
# model-facing (unlike the cursor/reading-animation FNs above), so it goes
# through the normal (guarded, approval-gated-if-configured) tool path, not
# the raw one.
_FORM_ERROR_CHECK_FN = (
    "() => {"
    "const sel = '[aria-invalid=\"true\"], .error, .error-message, .field-error, "
    "[role=\"alert\"], .toast-error, .invalid-feedback, .form-error, .Toastify__toast--error';"
    "const seen = new Set(); const results = [];"
    "document.querySelectorAll(sel).forEach((el) => {"
    "const text = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');"
    "if (!text || text.length > 200 || seen.has(text)) return;"
    "seen.add(text); results.push(text);"
    "});"
    "return JSON.stringify(results.slice(0, 8));"
    "}"
)

# Human-in-the-loop pause (login wall/CAPTCHA/2FA) -- see request_human_help
# below and README's "Human-in-the-loop pause" section for the full design.
# @playwright/mcp's browser_evaluate wraps its result as
# "### Result\n<value>\n### Ran Playwright code\n```js...```" (confirmed
# empirically against a live local session, same as every other raw
# browser_evaluate call in this file) -- a string result comes back
# JSON-quoted (e.g. '"https://example.com/login"'), a bare value (a number,
# an already-quote-free concatenation) doesn't. This helper undoes that so
# request_human_help's page-fingerprint polling gets a plain string back
# either way, not a JSON-quoted one it'd have to strip itself.
# NOTE the doubled backslashes: `str()` of the raw tool result is a Python
# repr of a tuple/list of dicts, so the *actual* newlines inside that
# result's own text come through as the literal two-character sequence
# backslash-n, not a real newline -- matching a real "\n" here would never
# find anything. Same doubled-backslash pattern already proven against a
# live session in this project's own /tmp/debug_reader.py and
# /tmp/test_reading_animation_live.py.
_EVALUATE_RESULT_RE = re.compile(r"### Result\\n(.*?)\\n### Ran", re.DOTALL)


def _parse_evaluate_text(raw: Any) -> str | None:
    text = str(raw)
    m = _EVALUATE_RESULT_RE.search(text)
    if not m:
        return None
    val = m.group(1).strip()
    if val.startswith('"') and val.endswith('"'):
        try:
            return json.loads(val)
        except Exception:  # noqa: BLE001
            return val
    return val


def _extract_eval_result_json(raw: Any) -> Any:
    text = str(raw)
    m = _EVALUATE_RESULT_RE.search(text)
    if m:
        val = m.group(1).strip()
    else:
        m2 = re.search(r"### Result\s*\n(.*?)\n### Ran", text, re.DOTALL)
        if m2:
            val = m2.group(1).strip()
        else:
            val = text
    if val.startswith('"') and val.endswith('"'):
        try:
            val = json.loads(val)
        except Exception:  # noqa: BLE001
            pass
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:  # noqa: BLE001
            pass
    return val


# Set-of-Marks JS snippet: scans visible interactive elements, computes stable selectors,
# and renders high-visibility numbered badges [1], [2], [3] directly on the live DOM.
_SET_OF_MARKS_FN = (
    "() => {"
    "  try {"
    "    document.querySelectorAll('.messa-som-badge').forEach(b => b.remove());"
    "    const selectors = ["
    "      'button', 'input:not([type=\"hidden\"])', 'select', 'textarea', 'a[href]',"
    "      '[role=\"button\"]', '[role=\"checkbox\"]', '[role=\"tab\"]', '[role=\"menuitem\"]',"
    "      '[role=\"link\"]', '[role=\"switch\"]', '[role=\"radio\"]', '[tabindex]:not([tabindex=\"-1\"])'"
    "    ];"
    "    const allElements = Array.from(document.querySelectorAll(selectors.join(',')));"
    "    const marks = [];"
    "    let markId = 1;"
    "    const isVisible = (el) => {"
    "      if (!el) return false;"
    "      const style = window.getComputedStyle(el);"
    "      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;"
    "      const rect = el.getBoundingClientRect();"
    "      if (rect.width <= 3 || rect.height <= 3) return false;"
    "      return rect.bottom >= 0 && rect.top <= (window.innerHeight || 800) &&"
    "             rect.right >= 0 && rect.left <= (window.innerWidth || 1280);"
    "    };"
    "    const getStableSelector = (el, tag, role, text, name, type) => {"
    "      if (el.id && !el.id.match(/^[0-9]/) && !el.id.includes(':') && document.querySelectorAll('#' + CSS.escape(el.id)).length === 1) {"
    "        return '#' + CSS.escape(el.id);"
    "      }"
    "      if (role && name) {"
    "        return 'role=' + role + '[name=\"' + name.replace(/\"/g, '\\\\\"') + '\"]';"
    "      }"
    "      const attrName = el.getAttribute('name');"
    "      if (attrName) {"
    "        return tag + '[name=\"' + attrName.replace(/\"/g, '\\\\\"') + '\"]';"
    "      }"
    "      const testId = el.getAttribute('data-testid') || el.getAttribute('data-test');"
    "      if (testId) {"
    "        return '[data-testid=\"' + testId + '\"]';"
    "      }"
    "      const placeholder = el.getAttribute('placeholder');"
    "      if (placeholder) {"
    "        return tag + '[placeholder=\"' + placeholder.replace(/\"/g, '\\\\\"') + '\"]';"
    "      }"
    "      const ariaLabel = el.getAttribute('aria-label');"
    "      if (ariaLabel) {"
    "        return '[aria-label=\"' + ariaLabel.replace(/\"/g, '\\\\\"') + '\"]';"
    "      }"
    "      if ((tag === 'button' || role === 'button' || tag === 'a') && text && text.length > 0 && text.length <= 40) {"
    "        return 'text=\"' + text.replace(/\"/g, '\\\\\"') + '\"';"
    "      }"
    "      if (tag === 'input' && type) {"
    "        return 'input[type=\"' + type + '\"]';"
    "      }"
    "      return tag;"
    "    };"
    "    for (const el of allElements) {"
    "      if (marks.length >= 60) break;"
    "      if (!isVisible(el)) continue;"
    "      const rect = el.getBoundingClientRect();"
    "      const tag = el.tagName.toLowerCase();"
    "      const role = el.getAttribute('role') || (tag === 'button' ? 'button' : (tag === 'a' ? 'link' : (tag === 'input' ? (['checkbox', 'radio'].includes(el.type) ? el.type : 'textbox') : '')));"
    "      const type = el.getAttribute('type') || '';"
    "      let label = '';"
    "      if (el.labels && el.labels.length > 0) label = el.labels[0].innerText.trim();"
    "      const text = (el.innerText || el.textContent || el.value || el.placeholder || el.getAttribute('aria-label') || label || '').trim().replace(/\\s+/g, ' ').slice(0, 50);"
    "      const accessibleName = el.getAttribute('aria-label') || label || text || el.placeholder || '';"
    "      const selector = getStableSelector(el, tag, role, text, accessibleName, type);"
    "      const badge = document.createElement('div');"
    "      badge.className = 'messa-som-badge';"
    "      badge.textContent = markId;"
    "      badge.style.position = 'fixed';"
    "      badge.style.left = Math.max(0, rect.left) + 'px';"
    "      badge.style.top = Math.max(0, rect.top) + 'px';"
    "      badge.style.background = '#ff0055';"
    "      badge.style.color = '#ffffff';"
    "      badge.style.fontSize = '11px';"
    "      badge.style.fontWeight = 'bold';"
    "      badge.style.fontFamily = 'monospace, sans-serif';"
    "      badge.style.padding = '1px 5px';"
    "      badge.style.borderRadius = '3px';"
    "      badge.style.zIndex = '2147483640';"
    "      badge.style.pointerEvents = 'none';"
    "      badge.style.boxShadow = '0 2px 5px rgba(0,0,0,0.5)';"
    "      badge.style.border = '1px solid #ffffff';"
    "      (document.body || document.documentElement).appendChild(badge);"
    "      marks.push({ id: markId, tag: tag, role: role || tag, type: type, text: text, selector: selector });"
    "      markId++;"
    "    }"
    "    setTimeout(() => {"
    "      document.querySelectorAll('.messa-som-badge').forEach(b => {"
    "        b.style.transition = 'opacity 500ms';"
    "        b.style.opacity = '0';"
    "        setTimeout(() => b.remove(), 500);"
    "      });"
    "    }, 15000);"
    "    return JSON.stringify(marks);"
    "  } catch(e) { return JSON.stringify([{error: e.toString()}]); }"
    "}"
)


# Deterministic backstop for human-help detection (per your answer: model-
# driven first, this is only the safety net). Deliberately a short, generic
# list -- this only needs to catch the OBVIOUS cases; anything subtler is
# exactly what the model itself is there to recognize. Checked against the
# text of the most recent successful browser_snapshot, not the live page
# (no extra browser round trip needed for a check that fires on every
# consecutive failure).
_AUTH_WALL_KEYWORDS = (
    "password", "verify you are human", "captcha", "two-factor", "2fa",
    "one-time code", "one-time passcode", "enter the code", "security check",
    "sign in to continue", "confirm your identity",
)


def _looks_like_auth_wall(snapshot_text: str | None) -> bool:
    if not snapshot_text:
        return False
    lowered = snapshot_text.lower()
    return any(kw in lowered for kw in _AUTH_WALL_KEYWORDS)


# agent-feedback.md item 2 -- code-level backstop for the OTP-invalidation
# bug, same "one corrective nudge" pattern as the anti-hallucination
# completion gate in `_run` below, just triggered by a different signal.
# The live-tested failure mode was NOT an explicit close_browser() call --
# it was the run ending its turn (a "done"/summary reply) while genuinely
# sitting on an OTP/verification screen, without ever calling
# await_email_verification_code. STAGEHAND_SYSTEM_PROMPT/
# DEEPSEARCH_SYSTEM_PROMPT now say explicitly not to do this, but a prompt
# is not enforcement on its own -- this is checked against the run's own
# FINAL message text (what's about to be reported back to Messa), not a
# live page read, so it costs no extra browser round trip. Deliberately a
# short, generic keyword list for the same reason _AUTH_WALL_KEYWORDS is:
# this only needs to catch the obvious "I'm on a verification screen"
# phrasing, not every possible wording.
_OTP_SCREEN_KEYWORDS = (
    "verification code", "one-time code", "one-time passcode", "otp",
    "enter the code", "check your email for", "check your inbox for",
    "confirmation code", "code sent to", "6-digit code", "4-digit code",
    "verify your email",
)


def _looks_like_pending_otp(text: str | None) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return any(kw in lowered for kw in _OTP_SCREEN_KEYWORDS)


_SNAPSHOT_SIZE_NUDGE = (
    "\n\n(This snapshot was large. Next time, prefer browser_find(text=...) for one "
    "specific element, or browser_snapshot(depth=...) for a shallower tree, instead of a "
    "full snapshot -- it's cheaper and faster, and you're carrying this one in full for "
    "the rest of the run.)"
)


def _snapshot_truncation_notice(kept: int, total: int) -> str:
    return (
        f"\n\n(This snapshot was cut off after {kept} of {total} characters -- it was well "
        "over the usual size, so the rest was dropped to keep this step fast. If what you "
        "need isn't in what you see above, use browser_find(text=...) for one specific "
        "element, or browser_snapshot(depth=...) for a shallower full-page tree, instead of "
        "the full snapshot.)"
    )


def _apply_snapshot_size_backstop(result: Any) -> Any:
    """Two-tier deterministic backstop for browser_snapshot's own size (see
    guarded()'s comment on when this is called, and
    config.DEEPSEARCH_LARGE_SNAPSHOT_CHARS/DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS
    for the two thresholds' own reasoning). `result` is @playwright/mcp's
    raw return shape for browser_snapshot -- a `(content_blocks, artifact)`
    tuple where content_blocks is a list of `{"type": "text", "text": ...}`
    dicts, confirmed by direct inspection of a real call, not assumed.

    - Under DEEPSEARCH_LARGE_SNAPSHOT_CHARS: returned unchanged.
    - Between the two thresholds: a corrective NUDGE is appended (asks the
      model to behave differently NEXT time), but the full content still
      reaches the model this call -- unchanged from before this tier
      existed.
    - Over DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS: the content itself is
      TRUNCATED to that many characters (collapsing multiple text blocks
      into one, since there's normally only one anyway) plus a clear notice
      explaining what happened and what to do next -- this is what actually
      shrinks THIS call's own cost/latency, not just future ones.

    Returns `result` unchanged (same object, not a defensive copy) whenever
    the shape isn't what's expected or the snapshot isn't actually
    oversized -- this must never be the reason a real snapshot result fails
    to reach the model."""
    if not isinstance(result, tuple) or len(result) != 2:
        return result
    content, artifact = result
    if not isinstance(content, list) or not content:
        return result
    total_len = sum(len(b.get("text", "")) for b in content if isinstance(b, dict))
    if total_len <= config.DEEPSEARCH_LARGE_SNAPSHOT_CHARS:
        return result
    if total_len > config.DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS:
        cap = config.DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS
        full_text = "".join(b.get("text", "") for b in content if isinstance(b, dict))
        truncated = full_text[:cap] + _snapshot_truncation_notice(cap, total_len)
        return ([{"type": "text", "text": truncated}], artifact)
    new_content = list(content)
    last = dict(new_content[-1])
    last["text"] = last.get("text", "") + _SNAPSHOT_SIZE_NUDGE
    new_content[-1] = last
    return (new_content, artifact)


def _target_values(name: str, kwargs: dict[str, Any]) -> list[str]:
    if name == "browser_drag":
        return [v for v in (kwargs.get("startTarget"), kwargs.get("endTarget")) if v]
    v = kwargs.get("target")
    return [v] if v else []


def _requires_fresh_snapshot(name: str, kwargs: dict[str, Any]) -> bool:
    """True only when every target this call touches looks like an opaque
    snapshot ref (or no target was given at all, the conservative default).
    A stable selector-style target skips the freshness requirement entirely
    -- see the module-level comment above _SNAPSHOT_REF_RE for why that's
    safe, and the README for the empirical verification behind it."""
    targets = _target_values(name, kwargs)
    if not targets:
        return True
    return any(_SNAPSHOT_REF_RE.match(t) for t in targets)


def _describe_action(name: str, args: dict[str, Any]) -> str:
    """Turns a raw Playwright MCP tool call into one human-readable line for
    the live-view page's description/chain-of-thought (see live_activity.py)
    -- e.g. "Navigating to https://..." instead of "browser_navigate". Best
    effort: an unrecognized tool name still gets a readable fallback rather
    than failing or showing the raw snake_case name."""
    kwargs = args if isinstance(args, dict) and "args" not in args else {}
    if name == "browser_navigate":
        url = kwargs.get("url")
        return f"Navigating to {url}" if url else "Navigating"
    if name == "browser_navigate_back":
        return "Going back"
    if name in ("browser_snapshot",):
        return "Reading the page"
    if name in ("browser_take_screenshot", "browser_screenshot"):
        return "Taking a screenshot"
    if name == "browser_click":
        return "Clicking on the page"
    if name == "browser_type":
        text = kwargs.get("text")
        return f'Typing "{text}"' if text else "Typing"
    if name == "browser_press_key":
        key = kwargs.get("key")
        return f"Pressing {key}" if key else "Pressing a key"
    if name == "browser_hover":
        return "Hovering over an element"
    if name == "browser_select_option":
        return "Selecting an option"
    if name in ("browser_wait_for", "browser_wait"):
        return "Waiting for the page"
    if name in ("browser_tab_new", "browser_new_tab"):
        return "Opening a new tab"
    if name in ("browser_tab_close", "browser_close"):
        return "Closing a tab"
    if name == "browser_drag":
        return "Dragging an element"
    if name == "browser_file_upload":
        return "Uploading a file"
    if name == "browser_evaluate":
        return "Running a script on the page"
    if name == "browser_handle_dialog":
        return "Responding to a dialog"
    if name == "browser_action_chain":
        acts = kwargs.get("actions") or []
        return f"Executing {len(acts)} action sequence" if acts else "Executing action sequence"
    if name == "browser_get_interactive_elements":
        return "Scanning interactive page elements"
    if "scroll" in name:
        return "Scrolling the page"
    return name.replace("browser_", "").replace("_", " ").strip().capitalize() or "Working on it"


def _domain_allowed(url: str) -> bool:
    if not config.DEEPSEARCH_ALLOWED_DOMAINS:
        return True
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in config.DEEPSEARCH_ALLOWED_DOMAINS)


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts)
    return str(content)


def _last_ai_text(messages: list[Any]) -> str:
    for m in reversed(messages):
        if getattr(m, "type", None) == "ai":
            text = _message_text(m)
            if text:
                return text
    return "(no summary produced)"


def _extract_last_target_url(messages: list[Any]) -> str | None:
    """Scan messages backwards to find the last visited HTTP/HTTPS URL that isn't about:blank."""
    for m in reversed(messages):
        tool_calls = getattr(m, "tool_calls", None) or []
        for tc in tool_calls:
            name = tc.get("name")
            args = tc.get("args") or {}
            if name in ("browser_navigate", "browser_navigate_back"):
                url = args.get("url")
                if url and isinstance(url, str) and url.startswith(("http://", "https://")) and not url.endswith("about:blank"):
                    return url
            if name == "browser_action_chain":
                for act in args.get("actions") or []:
                    if act.get("action") == "navigate":
                        url = act.get("url")
                        if url and isinstance(url, str) and url.startswith(("http://", "https://")) and not url.endswith("about:blank"):
                            return url
        content = getattr(m, "content", "")
        if isinstance(content, str):
            match = re.search(r"Page URL:\s*(https?://[^\s\n]+)", content)
            if match:
                found = match.group(1).strip()
                if not found.endswith("about:blank"):
                    return found
    return None


def sanitize_tool_messages(messages: list[Any]) -> list[Any]:
    """Heal a message list where an AIMessage's tool_calls were never
    followed by a matching ToolMessage -- the shape a run leaves behind
    whenever it's interrupted mid-tool-call: a crash, a cancellation, the
    step limit or a timeout firing while a tool call was in flight, or an
    unhandled exception inside provider._do(). Left unhealed, a message
    list like that breaks the very next completion call that includes it
    (OpenAI-style APIs reject an assistant tool_calls turn that isn't
    immediately answered by a tool result for every one of its ids) --
    which would otherwise surface as a hard failure the moment a paused
    session is either resumed (see the `messages_from_dict(...)` call
    building `prior_messages` above) or, before that, even just saved to
    the DB with a dangling tool call baked into its stored JSON.

    This only appends a synthetic ToolMessage for whichever specific
    tool_call ids are missing a real response -- it never removes or
    rewrites anything that already resolved correctly, so a healed session
    still remembers everything about the run except that one interrupted
    step.
    """
    healed: list[Any] = []
    for i, msg in enumerate(messages):
        healed.append(msg)
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            continue
        responded_ids: set[str] = set()
        j = i + 1
        while j < len(messages) and isinstance(messages[j], ToolMessage):
            responded_ids.add(messages[j].tool_call_id)
            j += 1
        for call in tool_calls:
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
            if call_id and call_id not in responded_ids:
                healed.append(ToolMessage(content="Action interrupted", tool_call_id=call_id))
    return healed


def _deepsearch_summarization_middleware(model: Any) -> SummarizationMiddleware | None:
    """Build the auto-compaction middleware deepsearch's own `create_agent`
    calls need explicitly -- unlike the four subagents built via deepagents'
    `create_deep_agent` (see config.py's big comment on
    SUBAGENT_EFFECTIVE_CONTEXT_TOKENS), deepsearch uses plain
    `langchain.agents.create_agent` directly (see this module's own two
    call sites), which does NOT attach any summarization middleware on its
    own -- its own `middleware=` parameter defaults to an empty tuple, full
    stop, not some quieter fallback. Confirmed by reading create_agent's
    signature: there is no hidden default here to rely on. That makes
    deepsearch -- specifically the agent most likely to need this, since it
    runs up to DEEPSEARCH_MAX_STEPS=100 steps and appends a fresh
    browser_snapshot on most of them -- the one place in this codebase that
    was running with ZERO compaction at all before this, not even the
    generic 170k-token fallback the other agents were quietly defaulting to.

    Trigger/keep are computed directly from
    config.SUBAGENT_EFFECTIVE_CONTEXT_TOKENS (deepsearch always runs on
    SUBAGENT_MODEL_NAME) using the same 85%-trigger/10%-keep split
    deepagents' own compute_summarization_defaults() uses for a model with a
    known profile -- rather than passing trigger=("fraction", 0.85") and
    leaning on model.profile being set, this computes the token counts
    directly, so it works even if a caller ever passes this a model instance
    config.build_model() didn't tag. No backend/offload here (contrast
    deepagents' own create_summarization_middleware, which writes evicted
    history to a filesystem backend the model can read_file back later):
    deepsearch's toolset has no filesystem tools at all, so an offloaded
    file would just be unreachable dead weight. Evicted detail becomes the
    LLM-written summary and nothing more -- acceptable here since
    deepsearch's whole job is to finish the task and report back a result,
    not to serve as a queryable transcript of exactly what it clicked 40
    steps ago.

    Returns `None` (skip compaction rather than error out) when `model`
    isn't an actual `BaseChatModel` instance -- production call sites
    always pass one (see config.build_model/registry.py), but this
    project's own test harness sometimes passes a bare string placeholder
    (e.g. `model="fake-model"`) to stand in for a real model without ever
    invoking it. `SummarizationMiddleware` tries to resolve a real provider
    from a string model at CONSTRUCTION time (confirmed directly -- it
    raises `ValueError: Unable to infer model provider for
    model='fake-model'` immediately, not lazily on first use), which would
    break exactly those tests for a code path they were never exercising in
    the first place.
    """
    if not isinstance(model, BaseChatModel):
        return None
    trigger_tokens = int(config.SUBAGENT_EFFECTIVE_CONTEXT_TOKENS * 0.85)
    keep_tokens = int(config.SUBAGENT_EFFECTIVE_CONTEXT_TOKENS * 0.10)
    return SummarizationMiddleware(
        model=model,
        trigger=("tokens", trigger_tokens),
        keep=("tokens", keep_tokens),
    )


class _TimingCallback(AsyncCallbackHandler):
    """Per-LLM-call latency logging -- added to answer the CTO-discussion
    question of WHY parallel delegate_website_task calls weren't producing
    the wall-clock speedup a naive Nx-concurrency estimate would suggest.
    `delegate_website_task` calls made in the same model turn genuinely run
    concurrently (LangGraph's own ToolNode, confirmed empirically -- see
    /tmp/test_agent_concurrency.py from this project's own history), so if a
    20-minute run isn't shrinking toward that, the missing time has to be
    going somewhere the concurrency mechanism itself can't see: a shared
    OpenRouter-key concurrency ceiling queuing "concurrent" LLM calls behind
    each other server-side, or the underlying browser/CPU being the actual
    bottleneck instead. This callback can't answer that by itself -- it just
    logs each LLM call's own wall-clock latency, tagged with `label` (e.g.
    the tab id or "top-level"), so a real run's logs show whether individual
    LLM calls slow down/queue when several sub-workers run at once (points
    at the shared-key theory) or stay roughly constant (points at
    browser/CPU contention instead, in which case a model/key pool wouldn't
    help and shouldn't be pursued further).

    AsyncCallbackHandler (not the sync BaseCallbackHandler) specifically
    because deepsearch's whole agent loop is async -- LangChain requires the
    async variant to get awaited correctly inside an async chain rather than
    silently running its sync fallback."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._starts: dict[Any, float] = {}

    async def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs: Any) -> None:  # noqa: D102
        self._starts[run_id] = time.monotonic()

    async def on_llm_end(self, response, *, run_id, **kwargs: Any) -> None:  # noqa: D102
        start = self._starts.pop(run_id, None)
        if start is not None:
            console.system(f"Deepsearch: [{self.label}] LLM call took {time.monotonic() - start:.2f}s")

    async def on_llm_error(self, error, *, run_id, **kwargs: Any) -> None:  # noqa: D102
        start = self._starts.pop(run_id, None)
        if start is not None:
            console.system(f"Deepsearch: [{self.label}] LLM call FAILED after {time.monotonic() - start:.2f}s: {error}")


# Round-robin cursor shared across a whole deepsearch run's
# delegate_website_task calls -- module-level (not per-provider-instance)
# purely so it doesn't reset if something ever constructs more than one
# top-level provider in the same process; in practice each run's own
# semaphore-bounded concurrency already caps how many keys are ever in use
# at once. See config.SUBAGENT_API_KEY_POOL's own comment for why this
# exists and what it's actually for.
_subagent_key_cycle = itertools.cycle(config.SUBAGENT_API_KEY_POOL)


def _pick_subagent_model(default_model: Any) -> Any:
    """Returns `default_model` unchanged when only one key is configured
    (SUBAGENT_API_KEY_POOL's default -- see its own comment), so this is a
    complete no-op until you actually add more keys. Otherwise builds a
    FRESH ChatOpenAI client (cheap -- no network call at construction time)
    bound to the next key in the pool, same model name and effective-
    context budget as `default_model` was built with, so each concurrent
    delegate_website_task call gets a different key without any other
    behavior changing."""
    if len(config.SUBAGENT_API_KEY_POOL) <= 1:
        return default_model
    return config.build_model(
        config.SUBAGENT_MODEL_NAME,
        effective_context_tokens=config.SUBAGENT_EFFECTIVE_CONTEXT_TOKENS,
        api_key=next(_subagent_key_cycle),
    )


class BrowserToolProvider:
    """Owns one Playwright MCP subprocess (connected to a remote Browserbase
    session over CDP) for the duration of an `async with` block.

    Usage:
        async with BrowserToolProvider(approval_gate, user_id=1) as provider:
            tools = provider.tools
            ...use tools while the block is open...
            provider.live_view_url  # link to watch this session live, or None
        # MCP subprocess is closed and the Browserbase session released here.
    """

    def __init__(
        self,
        approval_gate: ApprovalGate | None = None,
        user_id: int | None = None,
        deepsearch_session_id: int | None = None,
        *,
        server_url: str | None = None,
        model: BaseChatModel | None = None,
        messa_email: str | None = None,
        user_email: str | None = None,
        task_title: str | None = None,
        phone_number: str | None = None,
        live_view_share_url: str | None = None,
        initial_resume_url: str | None = None,
    ):
        self._approval_gate = approval_gate
        self._user_id = user_id
        self._initial_resume_url = initial_resume_url
        self._current_url: str = initial_resume_url or ""
        self._user_email = user_email
        # Owning-provider-only, for the "browser is actually live now" SMS
        # fired from inside _ensure_live_session -- see that method's own
        # comment for why the link moved here instead of going out with the
        # pre-delegation acknowledgment (cli.py). None for a sub-worker
        # (delegate_website_task never passes these -- a sub-worker's tab
        # opening isn't a user-facing event, only the top-level session is).
        self._phone_number = phone_number
        self._live_view_share_url = live_view_share_url
        # Owning-provider-only, for the lazy-open db.set_live_browser_active
        # call inside _ensure_live_session (see that method) -- moved there
        # from build_deepsearch_subagent._run's own body, since that call
        # now needs to fire whenever the session actually opens (possibly
        # well after __aenter__ returns), not right after __aenter__.
        self._task_title = task_title
        # For generate_account_credential's default username -- the same
        # "hand this out anywhere, I'll manage what comes back" address
        # already used for onboarding/personal-inbox signups (see
        # tools/personal_inbox_tools.py's own docstring). None if
        # migration 013 hasn't run or provisioning hasn't happened yet;
        # generate_account_credential requires an explicit username in
        # that case rather than guessing one.
        self._messa_email = messa_email
        # For db.create_human_help_request -- lets a human-help request row
        # be traced back to the deepsearch_sessions row it happened during.
        # None is fine (session tracking is itself optional, see
        # build_deepsearch_subagent's own "session tracking not enabled"
        # fallback) -- the human-help flow still works without it.
        self._deepsearch_session_id = deepsearch_session_id
        # A short random id identifying this one browser tab, independent of
        # deepsearch_session_id (which identifies the whole TASK, potentially
        # across resumed runs) -- load-bearing now that multi-site
        # delegation gives one deepsearch run several tabs at once, each
        # needing its own identity for telling one tab's human-help request/
        # live-activity entry apart from another's.
        self._tab_id = f"tab-{uuid.uuid4().hex[:10]}"
        # Multi-site delegation (see delegate_website_task below):
        # server_url is None for the TOP-LEVEL provider -- it owns the
        # Browserbase session and the @playwright/mcp HTTP server subprocess
        # backing it, created below in __aenter__. When server_url IS given
        # (only delegate_website_task constructs a provider this way), this
        # is a SUB-WORKER: it connects a brand-new, independent MCP client
        # to that ALREADY-RUNNING server instead of creating its own
        # Browserbase session or spawning a second server process -- which
        # is what makes several concurrent sub-workers cost one Browserbase
        # session, not N (see /tmp/test_mcp_multitab.py's empirical
        # confirmation that --shared-browser-context gives each separate
        # client connection its own isolated tab). `model` is only needed by
        # an owning provider (to run each sub-worker's own create_agent
        # loop) but is threaded through to sub-workers too since they
        # construct their OWN nested BrowserToolProvider today only via
        # delegate_website_task, which is unreachable from a sub-worker's
        # toolset (no recursive delegation, enforced by _owns_server below,
        # not just by omission from the tool list).
        self._server_url = server_url
        self._owns_server = server_url is None
        self._model = model
        self._mcp_proc: asyncio.subprocess.Process | None = None
        # Created only for an owning/top-level provider (see __aenter__) --
        # shared across every delegate_website_task call made during this
        # one deepsearch run, capping how many sub-worker tabs run at once
        # regardless of how many times the model calls the tool in one turn.
        self._subagent_semaphore: asyncio.Semaphore | None = None
        self._client: MultiServerMCPClient | None = None
        self._session_cm = None
        self._session = None
        self._bb_session_id: str | None = None
        self.live_view_url: str | None = None
        self.tools: list[BaseTool] = []
        # Lazy-open state (owning provider only -- see _ensure_live_session's
        # own docstring for why a sub-worker never needs this: it's always
        # constructed against an ALREADY-live server_url, by definition of
        # how delegate_website_task builds one). _live_session_ready is the
        # fast, lock-free check every guarded() call makes first; the lock
        # itself only matters for the (normally never-hit, since a model's
        # tool calls within one turn are dispatched by ToolNode which may
        # run them concurrently) case of two tool calls racing to be the
        # one that actually opens the session.
        self._session_open_lock: asyncio.Lock | None = None
        self._live_session_ready = False
        # Serializes actual tool EXECUTION on this one tab -- distinct from
        # _session_open_lock above, which only guards session CREATION.
        # Fix for the real production crash: "Attempted to exit cancel
        # scope in a different task than it was entered in" (anyio), which
        # happened when the model emitted two browser_navigate calls in the
        # same turn and ToolNode dispatched both concurrently against this
        # same tab's single MCP client connection -- the second navigation
        # aborted the first (net::ERR_ABORTED) and corrupted the shared
        # cancel scope, taking down the whole webhook turn (surfaced to the
        # user as a generic "something went wrong"). One lock per tab
        # instance (both the top-level tab and each sub-worker's own tab
        # get their own) means concurrent calls on the SAME tab now queue
        # instead of racing, while separate tabs (top-level vs. any
        # delegate_website_task sub-worker) stay fully parallel -- this is
        # deliberately NOT the same lock as _session_open_lock, and
        # deliberately initialized unconditionally (unlike that one) since
        # a sub-worker's own tab needs this exact same protection and there
        # is no lazy-open case to special-case around here.
        self._tool_call_lock = asyncio.Lock()
        # Raw (unwrapped) MCP tools by name, kept for internal use only --
        # currently just browser_evaluate, called directly by
        # _move_cursor_to without going through _guard()'s approval-gating
        # (that gate is for the MODEL choosing to run arbitrary JS; this is
        # our own cosmetic, non-model-initiated side effect and must never
        # prompt for or be blocked by human approval).
        self._raw_tools_by_name: dict[str, BaseTool] = {}
        # The in-flight background "reading" scroll animation (see
        # _start_reading_animation/_stop_reading_animation below), or None
        # when nothing is currently playing. Fire-and-forget by design --
        # nobody awaits this except _stop_reading_animation's own best-effort
        # cleanup, and __aexit__'s final sweep on the way out.
        self._reading_task: asyncio.Task | None = None
        # The in-flight background cursor-arrow move (see _move_cursor_to),
        # or None. Also fire-and-forget, same reasoning: awaiting the
        # arrow's own CSS-transition round trip used to add ~380ms of pure
        # latency in front of every real click/type/hover/select_option --
        # firing it as a background task instead means the real action
        # never waits on this purely cosmetic call. Tracked (one slot, most
        # recent move only -- there's only ever one meaningful cursor per
        # tab) purely so __aexit__ can cancel a still-in-flight one instead
        # of leaving a "Task was destroyed but it is pending" warning behind
        # on shutdown.
        self._cursor_move_task: asyncio.Task | None = None
        # Interactive Set-of-Marks mapping: marker number string ("1", "2") -> stable selector string
        self._interactive_markers: dict[str, str] = {}
        # Warm session flag: True when this provider is preserved in-memory across resumed turns
        self._is_warm = False
        # Shared mutable state referenced by the closures below.
        # last_snapshot_text: the most recent successful browser_snapshot's
        # result, truncated -- read by the deterministic human-help backstop
        # (_looks_like_auth_wall) so it doesn't need its own extra browser
        # round trip on every consecutive failure just to check.
        self._state = {"consecutive_errors": 0, "snapshot_fresh": False, "last_snapshot_text": None}

    async def _spawn_mcp_http_server(self, connect_url: str) -> str:
        """Owning provider only: launches @playwright/mcp as a local HTTP
        server (--port 0 lets it pick a free port) with
        --shared-browser-context, connected over CDP to the Browserbase
        session at `connect_url`. Returns the server's own base URL, parsed
        from its stdout -- confirmed empirically (see /tmp/test_mcp_multitab.py)
        that it prints a "Listening on http://..." line once ready, the same
        line this parses. --shared-browser-context is what lets
        delegate_website_task open brand-new, independent MCP client
        connections against this SAME server and each land on its own
        isolated tab within the one Browserbase session, instead of each
        sub-worker needing (and billing) its own session."""
        mcp_args = [
            "@playwright/mcp@latest", "--cdp-endpoint", connect_url,
            "--port", "0", "--shared-browser-context",
            # Deliberately NOT passing --isolated here (an earlier version
            # of this did). --isolated stops two connections from colliding
            # on the same page by giving each its own separate browser
            # CONTEXT -- but that also moves every sub-worker off
            # Browserbase's default context, which is the one thing its
            # live-view/debug-URL tracking follows end-to-end (see the
            # module docstring's "Multi-site delegation" section for the
            # full story and the empirical evidence). Instead, every
            # sub-worker claims its own tab WITHIN this one shared default
            # context via browser_tabs(action="new") as the first thing it
            # does on its own connection (see __aenter__ below) -- same
            # isolation guarantee, but a context Browserbase actually
            # tracks.
            # --image-responses omit: our subagent model
            # (config.SUBAGENT_MODEL_NAME) isn't confirmed to
            # accept image inputs, so a screenshot tool result
            # would otherwise embed a base64 image the model
            # can't actually use -- pure wasted tokens. This
            # also means we're deliberately NOT enabling
            # --caps=vision (the coordinate-click tools that
            # capability adds are useless without a
            # vision-capable model reading a screenshot first)
            # -- revisit only once the subagent model is
            # confirmed to support vision.
            "--image-responses", "omit",
            # See config.DEEPSEARCH_TIMEOUT_SETTLE_MS's comment
            # for why this is turned down from the package's
            # own 500ms default.
            "--timeout-settle", str(config.DEEPSEARCH_TIMEOUT_SETTLE_MS),
        ]
        # All the cosmetic live-view features (click/type cursor overlay,
        # click ripple, typing highlight, page-transition flash, reading
        # scroll animation) live in the same asset file and ride the same
        # --init-script injection, but are independently toggled -- only
        # skip injecting the script when ALL of them are off. A server-wide
        # flag (set once here, at the ONE process every tab -- top-level and
        # every delegated sub-worker alike -- ultimately connects through),
        # so every tab gets it automatically with no per-tab plumbing.
        init_scripts: list[str] = []
        if (
            config.DEEPSEARCH_CURSOR_OVERLAY
            or config.DEEPSEARCH_READING_ANIMATION
            or config.DEEPSEARCH_CLICK_RIPPLE
            or config.DEEPSEARCH_TYPING_HIGHLIGHT
            or config.DEEPSEARCH_PAGE_TRANSITION_FLASH
        ):
            init_scripts.append(str(_CURSOR_OVERLAY_SCRIPT_PATH))
        if config.DEEPSEARCH_AUTO_DISMISS_CONSENT:
            init_scripts.append(str(_CONSENT_DISMISS_SCRIPT_PATH))
        if init_scripts:
            # --init-script takes multiple paths (confirmed via `npx
            # @playwright/mcp@latest --help`: "<path...>", a variadic
            # option) -- one flag, every enabled script listed after it, so
            # cursor_overlay.js and deepsearch_enhancements.js can each be
            # toggled independently without stepping on each other.
            mcp_args += ["--init-script", *init_scripts]

        # env=dict(os.environ): a spawned subprocess does NOT inherit the
        # parent process's environment by default (deliberately -- so an
        # arbitrary MCP server doesn't automatically see your secrets).
        # Without PATH/HOME passed through, the spawned npx subprocess can't
        # even find node_modules/npx's own cache -- still needed here even
        # though the browser itself is remote now. asyncio.create_subprocess_exec
        # (not subprocess.Popen) so reading its stdout below doesn't block
        # the event loop -- other tool calls / the human-help poll loop
        # elsewhere in this process need to keep running while we wait for
        # this to come up.
        self._mcp_proc = await asyncio.create_subprocess_exec(
            "npx", *mcp_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=dict(os.environ),
        )
        deadline = asyncio.get_event_loop().time() + 20
        while asyncio.get_event_loop().time() < deadline:
            try:
                line = await asyncio.wait_for(self._mcp_proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                if self._mcp_proc.returncode is not None:
                    break
                continue
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                console.system(f"Deepsearch: [playwright-mcp] {text}")
            m = re.search(r"(http://\S+)", text)
            if m:
                return m.group(1)
        raise RuntimeError(
            "playwright-mcp HTTP server never printed a listening URL "
            f"(exited with code {self._mcp_proc.returncode})"
        )

    async def __aenter__(self) -> "BrowserToolProvider":
        if self._live_session_ready:
            # Warm session reuse: server, MCP client, and Browserbase session are already running
            self._is_warm = False
            console.system(f"Deepsearch: entered existing warm session (tab {self._tab_id}).")
            return self

        if self._owns_server:
            self._session_open_lock = asyncio.Lock()
            global _CACHED_TOOL_SPECS
            if _CACHED_TOOL_SPECS is not None:
                # The common case after the first deepsearch run in this
                # process's lifetime: build the FULL toolset right now, with
                # NO Browserbase session open yet -- opening one is deferred
                # to guarded()'s first real call (see _ensure_live_session's
                # own docstring). This is the actual fix for error.txt's
                # "browser sits open, billed, and idle through a 221s blind
                # planning call" finding: the model can see and call every
                # browser tool from the very first turn, but nothing is
                # actually billed until it calls one for real.
                self._build_tool_list(_CACHED_TOOL_SPECS)
                console.system(
                    f"Deepsearch: launched with {len(self.tools)} tools (top-level, browser "
                    "opens on first real tool call)."
                )
                return self
            # Cache miss -- no cached schema available yet (first run since
            # this process started, or a fresh deploy). No shortcut
            # possible: open for real right now, exactly like every run did
            # before this feature existed. This one real discovery also
            # POPULATES the cache (inside _ensure_live_session), so every
            # later run in this process's lifetime takes the fast path
            # above instead -- only ever the first task after a
            # restart/deploy pays this eager-open cost.
            await self._ensure_live_session()
            self._build_tool_list([(t.name, t.description, t.args_schema) for t in self._raw_tools_by_name.values()])
            console.system(
                f"Deepsearch: launched with {len(self.tools)} tools (top-level, cache miss -- "
                "opened eagerly)."
            )
            return self

        # Sub-worker: always eager, by construction -- delegate_website_task
        # never constructs one until the top-level session is already live,
        # so there's no idle-billing window here to optimize away.
        try:
            raw_tools = await self._connect_and_discover()
            self._build_tool_list([(t.name, t.description, t.args_schema) for t in raw_tools])
            console.system(
                f"Deepsearch: launched with {len(self.tools)} tools (sub-worker {self._tab_id})."
            )
        except Exception as e:  # noqa: BLE001
            console.tool_error(LABEL, "playwright_mcp_connect", str(e))
            raise
        return self

    def _build_tool_list(self, tool_specs: list[tuple[str, str, Any]]) -> None:
        """Builds self.tools from (name, description, args_schema) triples --
        shared by every path that ends up with a tool list: a sub-worker
        (always eager -- real specs from its own just-opened connection),
        the top-level provider on a cache miss (also real specs, from the
        connection _ensure_live_session just opened), and the top-level
        provider on a cache hit (CACHED specs, built here with no live
        connection at all yet). guarded() (built by _guard) is what
        actually triggers _ensure_live_session lazily on first real use, so
        this method itself never needs to know or care which of the three
        cases it's in -- it only ever writes schema-level tool objects."""
        EXCLUDED_MODEL_TOOLS = {
            "browser_evaluate",
            "browser_network_requests",
            "browser_network_request",
            "browser_run_code_unsafe",
        }
        self.tools = [
            self._guard(name, description, args_schema)
            for name, description, args_schema in tool_specs
            if name not in EXCLUDED_MODEL_TOOLS
        ]
        # request_human_help isn't a wrapped MCP tool -- it's our own Python
        # method, added directly to the model-facing toolset (unlike
        # _move_cursor_to/_start_reading_animation, which the model never
        # calls itself). Given to BOTH the top-level provider and every
        # sub-worker -- a login wall can turn up on any tab, not just the
        # first one. Its own body calls _ensure_live_session too (a no-op
        # for a sub-worker, already-eager by construction), so it's safe to
        # hand out here even before the top-level session is actually open.
        self.tools.append(StructuredTool.from_function(
            coroutine=self._request_human_help,
            name="request_human_help",
            description=(self._request_human_help.__doc__ or "").strip(),
        ))
        # Same "own Python method, given to every tab" reasoning as
        # request_human_help just above -- a verification-code screen can
        # turn up on any tab that's mid-signup, top-level or sub-worker.
        self.tools.append(StructuredTool.from_function(
            coroutine=self._await_email_verification_code,
            name="await_email_verification_code",
            description=(self._await_email_verification_code.__doc__ or "").strip(),
        ))
        # Same "own Python method, given to every tab" reasoning again -- a
        # form can be submitted on any tab, top-level or sub-worker. See
        # _check_form_errors's own docstring (faster-than-human.md's
        # "Upgrade 5").
        self.tools.append(StructuredTool.from_function(
            coroutine=self._check_form_errors,
            name="check_form_errors",
            description=(self._check_form_errors.__doc__ or "").strip(),
        ))
        # Same reasoning as request_human_help just above -- a signup or
        # login form can turn up on any tab, top-level or sub-worker. Neither
        # of these two touches the browser at all (pure DB/credential
        # operations), so neither needs (and neither triggers) a lazy open.
        self.tools.append(StructuredTool.from_function(
            coroutine=self._generate_account_credential,
            name="generate_account_credential",
            description=(self._generate_account_credential.__doc__ or "").strip(),
        ))
        self.tools.append(StructuredTool.from_function(
            coroutine=self._get_account_credential,
            name="get_account_credential",
            description=(self._get_account_credential.__doc__ or "").strip(),
        ))
        # Multi-provider instant web search (Tavily -> Brave -> Serper -> Parallel -> DuckDuckGo).
        # Completely independent of Browserbase -- answers search queries and finds candidate URLs
        # in <1 second without opening a browser or consuming browser session minutes.
        self.tools.append(StructuredTool.from_function(
            coroutine=_unified_web_search,
            name="search_web",
            description=(
                "Search the web instantly without opening a browser or using Browserbase session time. "
                "Powered by a fast multi-provider search engine (Tavily, Brave, Serper, Parallel, DuckDuckGo). "
                "Use this for looking up facts, finding URLs, researching options, or getting background info "
                "BEFORE deciding whether you need to navigate to a page. Returns titles, URLs, snippets, "
                "and direct answers in <1 second. Does NOT consume browser time."
            ),
        ))
        # Multi-provider instant web page reader (Jina Reader -> Firecrawl Cloud -> Parallel Fetch -> HTTP).
        # Renders JS server-side and costs ZERO browser session minutes.
        self.tools.append(StructuredTool.from_function(
            coroutine=_unified_web_read,
            name="read_webpage",
            description=(
                "Read a web page's rendered text content instantly WITHOUT using this browser session or tab. "
                "Powered by a remote reader waterfall (Jina Reader, Firecrawl Cloud, Parallel Fetch) that executes "
                "JavaScript server-side, costing NO Browserbase session time. Use this to read articles, compare "
                "options, or inspect content across several candidate URLs before deciding if you actually need "
                "interactive browser automation. Does NOT consume browser time."
            ),
        ))
        # Set-of-Marks visual index tool: returns numbered markers [1], [2], [3] for interactive elements
        self.tools.append(StructuredTool.from_function(
            coroutine=self._browser_get_interactive_elements,
            name="browser_get_interactive_elements",
            description=(
                "Scan visible interactive elements on the page (buttons, inputs, links, checkboxes) "
                "and display numbered markers [1], [2], [3] directly on-screen in the live view. "
                "Returns a concise list of elements and their stable selectors. You can target elements "
                "directly by their marker numbers (e.g. target='[1]' or target='1') in browser_click, "
                "browser_fill_form, browser_type, or browser_action_chain without taking a full page snapshot."
            ),
        ))
        # Compound multi-action tool: executes a fast sequence of browser actions in ONE turn
        # (e.g. accepting terms, clicking continue, and filling form fields) without separate LLM round-trips.
        self.tools.append(StructuredTool.from_function(
            coroutine=self._browser_action_chain,
            name="browser_action_chain",
            description=(
                "Execute a rapid compound chain of browser actions sequentially in ONE turn. "
                "Use this to perform multi-step flows (e.g. checking terms, clicking continue, "
                "and filling email/password fields) quickly like a human user without wasting round-trip turns. "
                "Accepts an array of action dicts: [{'action': 'click', 'target': 'role=checkbox', 'element': 'Terms'}, "
                "{'action': 'click', 'target': 'text=\"Continue with your Email\"'}, "
                "{'action': 'fill', 'target': 'role=textbox[name=\"Email\"]', 'value': 'user@example.com'}, "
                "{'action': 'select', 'target': 'role=combobox', 'values': ['Option']}, "
                "{'action': 'hover', 'target': 'role=menuitem'}, "
                "{'action': 'scroll', 'direction': 'down'}]. "
                "Supports target='[1]' marker numbers from browser_get_interactive_elements. "
                "Executes sequentially and returns the final page snapshot."
            ),
        ))
        # delegate_website_task is deliberately owning-provider-only --
        # _owns_server is the actual enforcement (not just leaving it off a
        # sub-worker's tool list), so there is no path to recursive
        # delegation even if this method were ever called directly. Its own
        # body calls _ensure_live_session first thing, same as
        # request_human_help above.
        if self._owns_server:
            self.tools.append(StructuredTool.from_function(
                coroutine=self._delegate_website_task,
                name="delegate_website_task",
                description=(self._delegate_website_task.__doc__ or "").strip(),
            ))

    async def _connect_and_discover(self) -> list[BaseTool]:
        """Opens a brand-new, independent MCP client connection against
        self._server_url (already-live for a sub-worker by construction;
        for the top-level provider, only called once its OWN Browserbase
        session + @playwright/mcp server are already up -- see
        _ensure_live_session and __aenter__'s cache-miss path) and
        discovers its real tool list. This is what gives each connection
        its own isolated tab within the shared browser context (confirmed
        empirically, see /tmp/test_mcp_multitab.py). Populates
        self._raw_tools_by_name and returns the raw tool list; does NOT
        touch self.tools -- callers decide separately whether/when to build
        the model-facing wrapped list (see _build_tool_list)."""
        self._client = MultiServerMCPClient({
            "playwright": {"url": self._server_url, "transport": "streamable_http"}
        })
        self._session_cm = self._client.session("playwright")
        self._session = await self._session_cm.__aenter__()
        raw_tools = await load_mcp_tools(self._session)
        self._raw_tools_by_name = {t.name: t for t in raw_tools}
        if not self._owns_server:
            # Claim our OWN tab within the shared default browser context,
            # replacing what the old --isolated flag used to guarantee (see
            # _spawn_mcp_http_server's comment and the module docstring's
            # "Multi-site delegation" section). Deliberately raised (not
            # swallowed) on failure -- this is the actual isolation
            # mechanism now, not a cosmetic nicety; a failure here must not
            # silently fall through to this connection sharing a tab with
            # someone else. __aenter__'s own except block reports it as a
            # normal failed delegate_website_task result rather than
            # crashing the run.
            tabs_tool = self._raw_tools_by_name.get("browser_tabs")
            if tabs_tool is not None:
                await tabs_tool.coroutine(action="new")
        return raw_tools

    async def _ensure_live_session(self) -> None:
        """Owning-provider-only: opens the REAL Browserbase session, spawns
        the REAL @playwright/mcp HTTP server against it, and connects the
        REAL MCP client -- exactly what __aenter__ used to do unconditionally
        and immediately, before this fix. Now called lazily, from inside a
        guarded tool call / request_human_help / delegate_website_task, the
        FIRST time any of them is actually invoked -- so a run that spends
        its first N seconds (or, per error.txt's real production trace,
        221+ seconds) purely planning, with no tool call yet, no longer pays
        for an idle, billed Browserbase session during that time.

        A no-op for a sub-worker (self._owns_server is False) -- a
        sub-worker is only ever constructed against an ALREADY-live
        server_url (delegate_website_task never builds one until the
        top-level session is confirmed open), so there's nothing lazy to do
        there; this just returns immediately, making it safe to call
        unconditionally from shared code (guarded(), request_human_help,
        delegate_website_task) without either of them needing to branch on
        which kind of provider they're running inside.

        Idempotent and safe under concurrent callers -- top-level tool calls
        are dispatched one at a time within a single model turn in
        practice, but this guards with a real asyncio.Lock rather than
        relying on that, in case ToolNode ever dispatches more than one
        top-level call concurrently."""
        global _CACHED_TOOL_SPECS
        if not self._owns_server or self._live_session_ready:
            return
        async with self._session_open_lock:
            if self._live_session_ready:  # re-check inside the lock
                return

            # Same two-stage error handling/cleanup this used to have
            # directly inside __aenter__ (see git history) -- moved here
            # unchanged, just later in wall-clock time. One Browserbase
            # Context per user, created once and reused forever, is what
            # makes logins survive between tasks.
            try:
                context_id = None
                if self._user_id is not None:
                    context_id = await db.get_browserbase_context_id(self._user_id)
                    if not context_id:
                        context_id = await browserbase.create_context()
                        await db.save_browserbase_context_id(self._user_id, context_id)

                session = await browserbase.create_session(context_id)
                self._bb_session_id = session["id"]
                connect_url = session["connectUrl"]
                console.system(f"Deepsearch: opened Browserbase session {self._bb_session_id}.")
                if self._user_id is not None:
                    live_activity.set_session_id(self._user_id, self._bb_session_id)
            except Exception as e:  # noqa: BLE001
                console.tool_error(LABEL, "browserbase_session_create", str(e))
                raise

            try:
                self.live_view_url = await browserbase.get_live_view_url(self._bb_session_id)
                if self._user_id is not None and self.live_view_url:
                    # Moved here from build_deepsearch_subagent._run, which
                    # used to call this right after __aenter__ returned --
                    # now that opening is lazy, the live-view dashboard
                    # should only flip to "browser active" once a browser
                    # genuinely is, which is exactly now.
                    await db.set_live_browser_active(
                        self._user_id, self.live_view_url, self._task_title or "Working on it",
                    )
                    # Fix for the "link arrives, browser doesn't open for 2
                    # minutes" complaint: the live-view link used to go out
                    # with cli.py's pre-delegation acknowledgment, the
                    # moment the model DECIDED to delegate -- well before a
                    # real Browserbase session exists (session creation,
                    # the CDP connect, and any planning LLM calls before the
                    # model's first real tool call all still had to happen
                    # after that text was already sent). Sending it HERE
                    # instead, right after db.set_live_browser_active
                    # confirms the live view is actually resolvable, means
                    # the text arrives exactly when there's something real
                    # to look at. Uses the constant per-user share URL
                    # (live_view_share_url, resolved dynamically by the
                    # dashboard's own status endpoint), not self.live_view_url
                    # itself -- same link cli.py used to send, just sent from
                    # here now, at the right time, and only once per run
                    # (this whole block only ever executes once -- see
                    # _ensure_live_session's own docstring). Best-effort: a
                    # failure here is logged, never lets a working browser
                    # session go to waste over an SMS delivery hiccup.
                    if self._phone_number and self._live_view_share_url:
                        try:
                            await sendblue.send_message(
                                self._phone_number,
                                f"Live now -- watch here: {self._live_view_share_url}",
                            )
                        except SendblueError as sms_err:
                            console.tool_error(LABEL, "live_link_sms", str(sms_err))
                    elif self._user_id is not None:
                        console.system(
                            f"Deepsearch: no live-link SMS sent for user #{self._user_id} -- "
                            f"phone_number={self._phone_number!r}, "
                            f"live_view_share_url={self._live_view_share_url!r}."
                        )
            except BrowserbaseError as e:
                # Best-effort: not having a live-view link yet shouldn't
                # block the run.
                console.tool_error(LABEL, "browserbase_live_view", str(e))

            try:
                self._server_url = await self._spawn_mcp_http_server(connect_url)
                self._subagent_semaphore = asyncio.Semaphore(config.DEEPSEARCH_MAX_SUBAGENTS)
                raw_tools = await self._connect_and_discover()
                if _CACHED_TOOL_SPECS is None:
                    _CACHED_TOOL_SPECS = [(t.name, t.description, t.args_schema) for t in raw_tools]

                # Automatic URL recovery on resumed sessions: if this session was continued from an earlier task
                # and opens at about:blank, immediately navigate back to the last visited URL.
                if self._initial_resume_url and config.DEEPSEARCH_AUTO_NAVIGATE_ON_RESUME:
                    nav_tool = self._raw_tools_by_name.get("browser_navigate")
                    if nav_tool is not None:
                        try:
                            console.system(
                                f"Deepsearch: auto-navigating resumed session from about:blank back to {self._initial_resume_url}"
                            )
                            await nav_tool.coroutine(url=self._initial_resume_url)
                            self._state["snapshot_fresh"] = False
                        except Exception as nav_err:
                            console.system(f"Deepsearch: auto-navigation to {self._initial_resume_url} failed: {nav_err}")
            except Exception as e:  # noqa: BLE001
                console.tool_error(LABEL, "browserbase_cdp_connect", str(e))
                # __aexit__ is never called for a failure here (this isn't
                # inside an `async with` block on the caller's side) --
                # without this, a Browserbase session that was created just
                # above but never got a working CDP connection would leak
                # (left running until it idles out on Browserbase's side,
                # rather than released immediately). Left unfixed, repeated
                # failed attempts would each leak another session -- on the
                # free plan's low concurrent-session cap, that alone could
                # make every *subsequent* attempt fail at
                # browserbase_session_create with a quota/limit error, which
                # would look identical to this failure from the outside.
                if self._mcp_proc is not None and self._mcp_proc.returncode is None:
                    self._mcp_proc.terminate()
                if self._bb_session_id is not None:
                    try:
                        await browserbase.release_session(self._bb_session_id)
                    except Exception as release_err:  # noqa: BLE001
                        console.tool_error(LABEL, "browserbase_release_after_failure", str(release_err))
                raise

            self._live_session_ready = True

    async def close(self) -> None:
        """Definitively closes the MCP subprocess, client session, and releases the Browserbase session."""
        if self._reading_task is not None and not self._reading_task.done():
            self._reading_task.cancel()
        if self._cursor_move_task is not None and not self._cursor_move_task.done():
            self._cursor_move_task.cancel()
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception as e:
                console.system(f"Deepsearch: suppressed session teardown error ({type(e).__name__}): {e}")
            self._session_cm = None
            self._session = None
        if not self._owns_server:
            console.system(f"Deepsearch: sub-worker tab {self._tab_id} closed.")
            return
        if self._mcp_proc is not None and self._mcp_proc.returncode is None:
            try:
                self._mcp_proc.terminate()
                await asyncio.wait_for(self._mcp_proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self._mcp_proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            self._mcp_proc = None
        if self._bb_session_id is not None:
            try:
                await browserbase.release_session(self._bb_session_id)
            except BrowserbaseError as e:
                console.tool_error(LABEL, "browserbase_release", str(e))
            console.system("Deepsearch: browser closed.")
            self._bb_session_id = None
        else:
            console.system("Deepsearch: run finished without ever opening a browser session.")
        self._live_session_ready = False

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._is_warm:
            console.system(
                f"Deepsearch: keeping browser session #{self._deepsearch_session_id} WARM in memory "
                "(skipping browser shutdown so subsequent turns or continuations retain tab state)."
            )
            return
        await self.close()

    def _fire_cursor_move(self, name: str, element: str | None, target: str | None) -> None:
        """Fire-and-forget entry point for the cosmetic cursor arrow (+
        click-ripple / typing-highlight, see cursor_overlay.js's
        window.__messaEffects) -- called from guarded() for every tool in
        CURSOR_ANIMATED_TOOLS. Deliberately NOT awaited: this used to be an
        inline `await self._move_cursor_to(...)` before the real action,
        which meant every click/type/hover/select_option paid the arrow's
        own ~380ms CSS-transition round trip as pure added latency in front
        of the real action -- a real, measurable slowdown for a purely
        cosmetic effect. Backgrounding it (same established pattern as
        _start_reading_animation) means the real action starts immediately;
        the arrow/ripple/highlight simply catch up a beat later, which is
        imperceptible on a live view but saves real wall-clock time on
        every single interaction across a run. Tracked in
        self._cursor_move_task (one slot -- only the most recent move
        matters) purely so __aexit__ can cancel a still-in-flight one
        instead of leaving a stray pending task behind on shutdown."""
        if not config.DEEPSEARCH_CURSOR_OVERLAY or not target:
            return
        self._cursor_move_task = asyncio.create_task(self._move_cursor_to(name, element, target))

    async def _move_cursor_to(self, name: str, element: str | None, target: str | None) -> None:
        """Best-effort, cosmetic-only: animate the injected SVG cursor (see
        cursor_overlay.js) to whatever element `target` resolves to, using
        the SAME target/ref the real action is about to use, so the arrow
        genuinely lands where the click/type will. For browser_click, also
        fires the click-ripple effect; for browser_type, also fires the
        typing-highlight -- both in the SAME evaluate() call as the arrow
        move (see _CURSOR_MOVE_CLICK_FN/_CURSOR_MOVE_TYPE_FN), so neither
        adds an extra round trip beyond what the arrow alone already costs.
        Calls the RAW browser_evaluate tool directly -- never the guarded/
        model-facing one -- so this never prompts for human approval and is
        invisible to the model entirely. Only ever invoked via
        _fire_cursor_move as a background task now (see that method's own
        docstring for why), so any failure here (element not found, no
        cursor script loaded, a slow page, or the tab having since closed)
        is swallowed exactly as before -- it just no longer has a real
        action waiting on it either way."""
        evaluate_tool = self._raw_tools_by_name.get("browser_evaluate")
        if evaluate_tool is None:
            return
        fn = {"browser_click": _CURSOR_MOVE_CLICK_FN, "browser_type": _CURSOR_MOVE_TYPE_FN}.get(name, _CURSOR_MOVE_FN)
        try:
            await evaluate_tool.coroutine(element=element or "target element", target=target, function=fn)
        except Exception as e:  # noqa: BLE001 - cosmetic only, never surfaced to the model
            console.system(f"Deepsearch: cursor overlay move failed (non-fatal): {e}")

    def _start_reading_animation(self) -> None:
        """Fire-and-forget: launches the background "reading" scroll
        animation right after a browser_snapshot succeeds. Deliberately NOT
        awaited here -- awaiting it would block the agent loop for however
        long the animation runs, defeating the entire point (this exists to
        fill dead time the agent loop was already going to spend thinking,
        not to add more). If one is somehow already running (shouldn't
        happen -- guarded() stops the previous one before any new action,
        and only browser_snapshot starts one), the JS side's own `running`
        flag makes a second start() call a safe no-op, so there's no need to
        guard against that here too."""
        if not config.DEEPSEARCH_READING_ANIMATION:
            return
        evaluate_tool = self._raw_tools_by_name.get("browser_evaluate")
        if evaluate_tool is None:
            return

        async def _run() -> None:
            try:
                await evaluate_tool.coroutine(
                    element="the page content", function=_READING_ANIMATION_START_FN,
                )
            except Exception as e:  # noqa: BLE001 - cosmetic only, never surfaced to the model
                console.system(f"Deepsearch: reading animation failed (non-fatal): {e}")

        self._reading_task = asyncio.create_task(_run())

    async def _stop_reading_animation(self) -> None:
        """Tells any in-flight reading animation to wind down, then forgets
        about it -- called at the top of every guarded() call (for every
        tool, not just the cursor-animated ones) since the model's next real
        action is a signal that "reading" is over, whatever that action is.
        Deliberately does NOT await self._reading_task itself: the JS side
        only checks its stop flag between hops, so the background call could
        still take up to one more hop/pause cycle to actually resolve --
        waiting for that here would reintroduce exactly the latency this
        feature isn't supposed to add. It's left to finish on its own time;
        __aexit__ cancels it outright if the whole session ends first.
        A no-op (zero extra MCP calls) when nothing is running, so ordinary
        tool calls pay no cost for a feature that never fired."""
        task = self._reading_task
        if task is None or task.done():
            return
        self._reading_task = None
        evaluate_tool = self._raw_tools_by_name.get("browser_evaluate")
        if evaluate_tool is None:
            return
        try:
            await evaluate_tool.coroutine(element="stop reading", function=_READING_ANIMATION_STOP_FN)
        except Exception as e:  # noqa: BLE001 - cosmetic only, never surfaced to the model
            console.system(f"Deepsearch: reading animation stop failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # live_activity dispatch: a sub-worker (delegate_website_task) reports
    # its own progress into live_activity's per-tab `tabs` dict instead of
    # the flat top-level fields, so several concurrent sub-workers never
    # race to clobber each other's (or the top-level's) description/steps/
    # waiting_for_human -- see live_activity.py's own comment above its
    # per-tab functions for the full rationale. The top-level provider
    # keeps using the flat fields exactly as before multi-site delegation
    # existed. guarded() and _request_human_help below call these instead
    # of live_activity.* directly so neither has to branch on
    # self._is_subworker itself.
    # ------------------------------------------------------------------

    def _live_set_description(self, text: str) -> None:
        if self._user_id is None:
            return
        if self._owns_server:
            live_activity.set_description(self._user_id, text)
        else:
            live_activity.set_tab_description(self._user_id, self._tab_id, text)

    def _live_add_step(self, text: str) -> None:
        if self._user_id is None:
            return
        if self._owns_server:
            live_activity.add_step(self._user_id, text)
        else:
            live_activity.add_tab_step(self._user_id, self._tab_id, text)

    def _live_set_waiting(self, reason: str) -> None:
        if self._user_id is None:
            return
        if self._owns_server:
            live_activity.set_waiting_for_human(self._user_id, reason)
        else:
            live_activity.set_tab_waiting_for_human(self._user_id, self._tab_id, reason)

    def _live_clear_waiting(self) -> None:
        if self._user_id is None:
            return
        if self._owns_server:
            live_activity.clear_waiting_for_human(self._user_id)
        else:
            live_activity.clear_tab_waiting_for_human(self._user_id, self._tab_id)

    def _live_mark_active(self) -> None:
        """Called at the top of every guarded() call (this tab is the one
        doing something right now) -- feeds server.py's "follow whichever
        tab is actually active" live-view logic (see
        live_activity.set_active_tab's own docstring)."""
        if self._user_id is None:
            return
        live_activity.set_active_tab(self._user_id, None if self._owns_server else self._tab_id)

    def _live_set_url(self, url: str) -> None:
        """Records this tab's current url after a successful navigation --
        matched later against Browserbase's own pages[] (by url) to resolve
        this specific tab's live-view debug link. See
        channels/browserbase.get_session_pages and server.py's status route."""
        self._current_url = url
        if self._user_id is None:
            return
        if self._owns_server:
            live_activity.set_url(self._user_id, url)
        else:
            live_activity.set_tab_url(self._user_id, self._tab_id, url)

    async def _page_fingerprint(self) -> int | None:
        """A cheap, no-side-effect check of "has anything changed" for
        request_human_help's polling loop: the current URL plus the length
        of every input field's value, joined into one string. Deliberately
        NOT a full browser_snapshot (that's a much heavier call to run every
        few seconds for up to several minutes) -- just enough to notice (a)
        the page navigated (a login/verification usually redirects on
        success) or (b) someone is actively typing into a field, without
        reading the actual field contents. Returns None on any failure
        (e.g. the tab isn't ready yet) rather than raising -- a missed poll
        just means the next one tries again.

        Deliberately returns a single PLAIN NUMBER, not a string: a string
        result (even a boring one like a bare URL) gets JSON-quoted by
        browser_evaluate, and if that string happens to contain both a
        single and a double quote (a realistic URL usually doesn't, but the
        "Ran Playwright code" example block wrapped around every
        browser_evaluate result sometimes does), Python's own repr() of the
        surrounding structure escapes it unpredictably -- confirmed the hard
        way while building this (see /tmp/test_human_help.py's history).
        Encoding "URL identity" as a bounded hash and "how much text is in
        every input field" as a bounded length, packed into one integer,
        sidesteps that whole class of parsing fragility -- a bare number's
        str() is unambiguous no matter what it's nested inside."""
        evaluate_tool = self._raw_tools_by_name.get("browser_evaluate")
        if evaluate_tool is None:
            return None
        try:
            raw = await evaluate_tool.coroutine(
                element="human-help page check",
                function=(
                    "() => {"
                    "let h = 0;"
                    "for (let i = 0; i < document.URL.length; i++) {"
                    "h = (h * 31 + document.URL.charCodeAt(i)) | 0;"
                    "}"
                    "h = Math.abs(h) % 1000000;"
                    "const totalLen = Math.min(Array.from(document.querySelectorAll('input'))"
                    ".reduce((a, el) => a + (el.value || '').length, 0), 999);"
                    "return h * 1000 + totalLen;"
                    "}"
                ),
            )
            text = _parse_evaluate_text(raw)
            return int(text) if text is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _sanitize_typed_text(self, text: str, element_desc: str | None, current_url: str | None = None) -> str:
        """Inspects text being typed into the browser. If the agent attempts to type an
        unauthorized or hallucinated email address into an account/signup/login field or auth page,
        automatically substitutes the user's authoritative Messa email address."""
        if not text or not self._messa_email or "@" not in text:
            return text

        email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
        matches = re.findall(email_pattern, text)
        if not matches:
            return text

        elem_str = (element_desc or "").lower()
        url_str = (current_url or self._current_url or "").lower()

        # Check if the field or context is an account/email/login/signup field
        is_auth_field = any(kw in elem_str for kw in (
            "email", "phone number or email", "username", "login", "sign in", "signin",
            "sign up", "signup", "register", "auth", "account", "user", "e-mail"
        ))
        is_auth_page = any(kw in url_str for kw in (
            "auth", "login", "signin", "sign-in", "signup", "sign-up", "register", "account"
        ))

        # Do not intercept if explicitly a generic content search box unless on an auth page
        if any(kw in elem_str for kw in ("search", "find restaurants", "search box")) and not is_auth_page:
            return text

        sanitized = text
        for email in matches:
            email_lower = email.lower()
            allowed = {self._messa_email.lower()}
            if self._user_email:
                allowed.add(self._user_email.lower())

            if email_lower not in allowed:
                console.system(
                    f"Deepsearch Guardrail: intercepted hallucinated email '{email}', "
                    f"substituting authorized Messa email '{self._messa_email}'"
                )
                sanitized = sanitized.replace(email, self._messa_email)

        return sanitized

    def _resolve_target(self, target: Any) -> Any:
        """Resolves Set-of-Marks markers (e.g. '[1]', '1', '#1', 'marker 1') to stable Playwright selectors."""
        if not target or not isinstance(target, str):
            return target
        t = target.strip()
        m = re.match(r"^\[?#?(?:mark(?:er)?[:\s]*)?(\d+)\]?$", t, re.IGNORECASE)
        if m:
            num = m.group(1)
            if hasattr(self, "_interactive_markers") and num in self._interactive_markers:
                resolved = self._interactive_markers[num]
                console.system(f"Deepsearch: resolved marker [{num}] to selector '{resolved}'")
                return resolved
        return target

    async def _browser_get_interactive_elements(self, focus_area: str | None = None) -> str:
        """Model-facing tool: Scan visible interactive elements on the page and display
        numbered badges [1], [2], [3] directly on-screen in the live view.

        Returns an indexed list of interactive elements (buttons, inputs, links, checkboxes)
        with their stable selectors and markers. You can use these markers directly as targets
        (e.g. target="[1]" or target="1") in browser_click, browser_fill_form, browser_type,
        or browser_action_chain.
        """
        await self._ensure_live_session()
        await self._stop_reading_animation()

        evaluate_tool = self._raw_tools_by_name.get("browser_evaluate")
        if not evaluate_tool:
            return "ERROR: browser_evaluate tool is unavailable."

        self._live_set_description("Scanning interactive elements")
        self._live_mark_active()

        try:
            raw = await evaluate_tool.coroutine(
                element="interactive elements",
                function=_SET_OF_MARKS_FN,
            )
            parsed = _extract_eval_result_json(raw)
            if isinstance(parsed, str):
                try:
                    parsed = json.loads(parsed)
                except Exception:
                    pass

            if not isinstance(parsed, list) or not parsed:
                return "No visible interactive elements detected on the current page. Try taking a browser_snapshot."

            self._interactive_markers = {}
            lines = [f"Found {len(parsed)} visible interactive elements on the page (marked on-screen [1] to [{len(parsed)}]):"]
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                if "error" in item:
                    return f"ERROR scanning elements: {item['error']}"
                mid = str(item.get("id"))
                selector = item.get("selector", "")
                self._interactive_markers[mid] = selector

                tag = item.get("tag", "element")
                role = item.get("role", "")
                typ = item.get("type", "")
                text = item.get("text", "").strip()
                desc_parts = [role or tag]
                if typ:
                    desc_parts.append(f"type={typ}")
                if text:
                    desc_parts.append(f'"{text}"')

                lines.append(f"[{mid}] {' '.join(desc_parts)} -> selector: {selector}")

            lines.append("\nTip: Target elements directly by marker number (e.g. target=\"[1]\" or target=\"1\") in browser_click, browser_type, browser_fill_form, or browser_action_chain!")
            return "\n".join(lines)
        except Exception as e:
            return f"ERROR scanning interactive elements: {e}"

    async def _browser_action_chain(self, actions: list[dict[str, Any]]) -> str:
        """Model-facing tool: execute a rapid compound chain of browser actions sequentially
        in ONE turn. Use this to handle multi-step interactions (e.g. accepting terms, clicking
        continue, filling out email & password, or submitting a form) without waiting for
        separate LLM round-trips.

        Args:
            actions: An array of action dicts executed in order. Supported actions:
              - {"action": "click", "target": "role=checkbox", "element": "Terms and conditions"}
              - {"action": "fill", "target": "role=textbox[name='Email']", "value": "user@textmessa.com"}
              - {"action": "type", "target": "role=textbox[name='Password']", "text": "secret"}
              - {"action": "press", "key": "Enter"}
              - {"action": "wait", "time": 1.5}
              - {"action": "select", "target": "role=combobox", "values": ["US"]}
              - {"action": "hover", "target": "role=menuitem"}
              - {"action": "scroll", "direction": "down"}
              - {"action": "navigate", "url": "https://..."}

        Returns:
            A summary of executed steps and a fresh snapshot of the resulting page state.
        """
        if not actions or not isinstance(actions, list):
            return "ERROR: 'actions' must be a non-empty list of action dicts."

        await self._ensure_live_session()
        await self._stop_reading_animation()

        executed: list[str] = []
        action_summary = f"Executing {len(actions)} actions in sequence"
        console.tool_call(LABEL, "browser_action_chain", {"actions_count": len(actions)})
        self._live_set_description(action_summary)
        self._live_mark_active()

        async with self._tool_call_lock:
            for idx, act in enumerate(actions):
                if not isinstance(act, dict):
                    continue
                act_type = str(act.get("action") or "").strip().lower()
                raw_target = act.get("target")
                target = self._resolve_target(raw_target) if raw_target else None
                element = act.get("element") or act.get("name") or "element"
                desc = f"{act_type}({element or target or ''})"

                try:
                    for attempt in range(2):
                        if act_type == "click":
                            if not target:
                                return f"ERROR: 'target' is required for click action at step {idx+1}."
                            if config.DEEPSEARCH_CURSOR_OVERLAY and target:
                                self._fire_cursor_move("browser_click", element, target)
                                await asyncio.sleep(0.18)
                            tool = self._raw_tools_by_name.get("browser_click")
                            if not tool:
                                return "ERROR: browser_click tool is unavailable."
                            res = await tool.coroutine(target=target, element=element)

                        elif act_type == "fill":
                            tool = self._raw_tools_by_name.get("browser_fill_form")
                            if not tool:
                                return "ERROR: browser_fill_form tool is unavailable."
                            if "fields" in act and isinstance(act["fields"], list):
                                for f in act["fields"]:
                                    if isinstance(f, dict):
                                        if "target" in f:
                                            f["target"] = self._resolve_target(f["target"])
                                        if "value" in f:
                                            f["value"] = self._sanitize_typed_text(str(f["value"]), f.get("name"), self._current_url)
                                if config.DEEPSEARCH_CURSOR_OVERLAY and act["fields"]:
                                    first_target = act["fields"][0].get("target")
                                    if first_target:
                                        self._fire_cursor_move("browser_type", act["fields"][0].get("name"), first_target)
                                        await asyncio.sleep(0.15)
                                res = await tool.coroutine(fields=act["fields"])
                            elif target:
                                if config.DEEPSEARCH_CURSOR_OVERLAY and target:
                                    self._fire_cursor_move("browser_type", element, target)
                                    await asyncio.sleep(0.15)
                                val = str(act.get("value") or act.get("text") or "")
                                val = self._sanitize_typed_text(val, element, self._current_url)
                                res = await tool.coroutine(fields=[{"target": target, "value": val, "name": element, "type": "textbox"}])
                            else:
                                return f"ERROR: 'target' or 'fields' is required for fill action at step {idx+1}."

                        elif act_type == "type":
                            if not target:
                                return f"ERROR: 'target' is required for type action at step {idx+1}."
                            if config.DEEPSEARCH_CURSOR_OVERLAY and target:
                                self._fire_cursor_move("browser_type", element, target)
                                await asyncio.sleep(0.15)
                            tool = self._raw_tools_by_name.get("browser_type")
                            if not tool:
                                return "ERROR: browser_type tool is unavailable."
                            text = str(act.get("text") or act.get("value") or "")
                            text = self._sanitize_typed_text(text, element, self._current_url)
                            res = await tool.coroutine(target=target, text=text, element=element)

                        elif act_type in ("select", "select_option"):
                            if not target:
                                return f"ERROR: 'target' is required for select action at step {idx+1}."
                            if config.DEEPSEARCH_CURSOR_OVERLAY and target:
                                self._fire_cursor_move("browser_select_option", element, target)
                                await asyncio.sleep(0.15)
                            tool = self._raw_tools_by_name.get("browser_select_option")
                            if not tool:
                                return "ERROR: browser_select_option tool is unavailable."
                            vals = act.get("values") or [act.get("value")]
                            res = await tool.coroutine(target=target, values=vals, element=element)

                        elif act_type == "hover":
                            if not target:
                                return f"ERROR: 'target' is required for hover action at step {idx+1}."
                            if config.DEEPSEARCH_CURSOR_OVERLAY and target:
                                self._fire_cursor_move("browser_hover", element, target)
                                await asyncio.sleep(0.15)
                            tool = self._raw_tools_by_name.get("browser_hover")
                            if not tool:
                                return "ERROR: browser_hover tool is unavailable."
                            res = await tool.coroutine(target=target, element=element)

                        elif act_type == "scroll":
                            tool = self._raw_tools_by_name.get("browser_scroll")
                            if not tool:
                                return "ERROR: browser_scroll tool is unavailable."
                            direction = str(act.get("direction") or "down").lower()
                            res = await tool.coroutine(direction=direction)

                        elif act_type == "press":
                            tool = self._raw_tools_by_name.get("browser_press_key")
                            if not tool:
                                return "ERROR: browser_press_key tool is unavailable."
                            key = act.get("key") or "Enter"
                            res = await tool.coroutine(key=key)

                        elif act_type == "wait":
                            tool = self._raw_tools_by_name.get("browser_wait_for")
                            if not tool:
                                return "ERROR: browser_wait_for tool is unavailable."
                            t = float(act.get("time") or 1.0)
                            res = await tool.coroutine(time=t)

                        elif act_type == "navigate":
                            url = act.get("url")
                            if not url:
                                return f"ERROR: 'url' is required for navigate action at step {idx+1}."
                            if not _domain_allowed(url):
                                return f"BLOCKED: '{url}' is not in the allowed domain list."
                            tool = self._raw_tools_by_name.get("browser_navigate")
                            if not tool:
                                return "ERROR: browser_navigate tool is unavailable."
                            res = await tool.coroutine(url=url)

                        else:
                            return f"ERROR: Unsupported action '{act_type}' at step {idx+1}. Supported: click, fill, type, select, hover, scroll, press, wait, navigate."

                        res_str = str(res)
                        # Transient delay check: if target was not found or timed out, retry once after a short settle
                        is_transient = (
                            ("TimeoutError" in res_str or "does not match any elements" in res_str or "Ref not found" in res_str)
                            and act_type in ("click", "fill", "type", "select", "select_option", "hover")
                        )
                        if is_transient and attempt == 0:
                            console.system(f"Deepsearch: action '{desc}' transiently failed ({res_str[:80]}), retrying once after 0.4s...")
                            await asyncio.sleep(0.4)
                            continue
                        break

                    if (
                        res_str.startswith("ERROR:")
                        or "### Error" in res_str
                        or "TimeoutError" in res_str
                        or "does not match any elements" in res_str
                        or "Ref not found" in res_str
                    ):
                        snap_tool = self._raw_tools_by_name.get("browser_snapshot")
                        snap = await snap_tool.coroutine(depth=8) if snap_tool else ""
                        return (
                            f"Action chain halted at step {idx+1}/{len(actions)} ({desc}): {res_str}\n"
                            f"Steps executed successfully: {', '.join(executed) if executed else 'None'}\n\n"
                            f"Current page snapshot:\n{snap}"
                        )

                    executed.append(desc)
                    self._live_add_step(desc)
                    await asyncio.sleep(0.25)

                except Exception as e:
                    snap_tool = self._raw_tools_by_name.get("browser_snapshot")
                    snap = await snap_tool.coroutine(depth=8) if snap_tool else ""
                    return (
                        f"Action chain failed with exception at step {idx+1}/{len(actions)} ({desc}): {e}\n"
                        f"Steps executed successfully: {', '.join(executed) if executed else 'None'}\n\n"
                        f"Current page snapshot:\n{snap}"
                    )

        self._state["snapshot_fresh"] = True
        snap_tool = self._raw_tools_by_name.get("browser_snapshot")
        final_snap = await snap_tool.coroutine(depth=8) if snap_tool else ""
        return (
            f"Successfully executed all {len(actions)} actions: {', '.join(executed)}.\n\n"
            f"Current page snapshot:\n{final_snap}"
        )

    async def _await_email_verification_code(
        self, sender_keyword: str = "", timeout_seconds: int | None = None
    ) -> str:
        """Model-facing tool: call this the moment you reach an email
        verification/OTP screen DURING ACCOUNT CREATION, specifically when
        the account was signed up using the user's own Messa email address
        (generate_account_credential's default username) -- NOT when the
        code went to the user's personal email or phone number, which only
        the user can read (use request_human_help for those instead, since
        there's a real human who can act on it).

        Registers a wait for the verification email and keeps this tab's
        browser session open while it waits. Messa's own inbound-email
        webhook feeds the code back the moment the real email arrives --
        usually within a few seconds -- with NO text sent to the user at
        all; this is meant to be fully autonomous. Returns "RESOLVED: <the
        code>" the moment it arrives, or "TIMEOUT: ..." if nothing showed
        up within the wait window -- fall back to request_human_help if
        that happens (e.g. the site actually sent it to the user's own
        phone instead).

        sender_keyword: a short, distinctive fragment of the sending
        domain/address or subject you expect (e.g. "uber", "airbnb") --
        narrows matching so a concurrent, unrelated verification email for
        a DIFFERENT site/task doesn't get consumed by this wait. Leave
        blank only if you genuinely have no better guess.

        timeout_seconds: optional override, capped at
        config.DEEPSEARCH_OTP_WAIT_MAX_SECONDS regardless -- most sites
        send the email within 10-20s, so the default is already generous."""
        if self._user_id is None:
            return "ERROR: await_email_verification_code isn't available outside a real user session."
        if not self._messa_email:
            return (
                "ERROR: this user has no Messa email address provisioned, so this tool can't "
                "apply -- it only works for codes sent to the user's own Messa-assigned inbox. "
                "If the code went to the user's personal email or phone instead, use "
                "request_human_help."
            )

        await self._ensure_live_session()
        await self._stop_reading_animation()

        wait_seconds = config.DEEPSEARCH_OTP_WAIT_MAX_SECONDS
        if timeout_seconds is not None:
            try:
                wait_seconds = max(10, min(int(timeout_seconds), config.DEEPSEARCH_OTP_WAIT_MAX_SECONDS))
            except (TypeError, ValueError):
                pass

        console.system(
            f"Deepsearch: awaiting email verification code on tab {self._tab_id} "
            f"(sender_keyword={sender_keyword!r}, up to {wait_seconds}s)."
        )

        # 1. Immediate lookback: check if a matching email arrived in the last 3 minutes
        # (catches the common race where the site emailed the code in 1-2s upon clicking
        # submit, while the LLM spent 10-20s generating the next tool call).
        recent = await db.find_recent_otp_in_inbox(self._user_id, sender_keyword, max_age_seconds=180)
        if recent and recent.get("code"):
            console.system(
                f"Deepsearch: found recent verification code {recent['code']} already in inbox on tab {self._tab_id}."
            )
            self._live_add_step("Received verification code")
            return f"RESOLVED: Received verification code {recent['code']}. Enter it now."

        request_row = await db.create_otp_expectation(
            self._user_id, self._deepsearch_session_id, self._tab_id, sender_keyword,
        )
        if request_row is None:
            return (
                "ERROR: verification-code waiting isn't available yet (migrations/028 hasn't been "
                "applied) -- fall back to request_human_help instead."
            )
        expectation_id = request_row["id"]
        self._live_set_waiting(f"Waiting for verification code ({sender_keyword or 'email'})")

        try:
            elapsed = 0
            while elapsed < wait_seconds:
                await asyncio.sleep(config.DEEPSEARCH_OTP_WAIT_POLL_INTERVAL_SECONDS)
                elapsed += config.DEEPSEARCH_OTP_WAIT_POLL_INTERVAL_SECONDS
                row = await db.get_otp_expectation(expectation_id)
                if row and row.get("status") == "resolved" and row.get("code"):
                    console.system(
                        f"Deepsearch: verification code received on tab {self._tab_id} after {elapsed}s."
                    )
                    self._live_add_step("Received verification code")
                    return f"RESOLVED: Received verification code {row['code']}. Enter it now."

                # Backstop check: directly query inbox during wait loop in case webhook didn't resolve expectation
                inbox_check = await db.find_recent_otp_in_inbox(self._user_id, sender_keyword, max_age_seconds=120)
                if inbox_check and inbox_check.get("code"):
                    console.system(
                        f"Deepsearch: picked up verification code {inbox_check['code']} directly from inbox on tab {self._tab_id} after {elapsed}s."
                    )
                    await db.expire_otp_expectation(expectation_id)
                    self._live_add_step("Received verification code")
                    return f"RESOLVED: Received verification code {inbox_check['code']}. Enter it now."

                if row and row.get("status") == "expired":
                    # Extremely unlikely (nothing else expires this row while
                    # we're the one still polling it) but handled for
                    # completeness -- treat it exactly like a timeout below.
                    break
            await db.expire_otp_expectation(expectation_id)
            return (
                f"TIMEOUT: no verification email arrived within {wait_seconds}s. Take a fresh "
                "browser_snapshot to check the page's current state, and consider whether the "
                "code may have gone to the user's own phone/email instead -- if so, call "
                "request_human_help."
            )
        finally:
            self._live_mark_active()

    async def _check_form_errors(self) -> str:
        """Model-facing tool: call this immediately after clicking a form's
        submit/continue/create-account button, BEFORE taking a full
        browser_snapshot to see whether it worked. Runs one small, targeted
        DOM scan for common validation-error markers (aria-invalid fields,
        .error/.field-error classes, role="alert" toasts) and returns just
        their text -- much cheaper and more reliable than spotting an error
        message by eye inside a whole-page snapshot, and it catches errors
        some sites show without moving focus or changing the URL at all.

        Returns "NO_ERRORS_FOUND: ..." if nothing matched (the submission
        likely went through, or this site doesn't surface errors this way --
        take a normal browser_snapshot next to confirm either way), or
        "FORM_ERRORS: " followed by the matched text if something turned up
        -- fix what it describes and resubmit. Per the EARLY ESCALATION
        rule: if you see the SAME error again after already trying to fix
        it, stop and call request_human_help instead of retrying a third
        time."""
        evaluate_tool = self._raw_tools_by_name.get("browser_evaluate")
        if evaluate_tool is None:
            return "NO_ERRORS_FOUND: form-error checking isn't available on this session -- take a browser_snapshot instead."
        try:
            raw = await evaluate_tool.coroutine(element="the page", function=_FORM_ERROR_CHECK_FN)
        except Exception as e:  # noqa: BLE001 - falls back to the normal snapshot path, never blocks the agent
            console.system(f"Deepsearch: check_form_errors evaluate failed (non-fatal): {e}")
            return "NO_ERRORS_FOUND: the check itself failed -- take a browser_snapshot instead."

        parsed = _extract_eval_result_json(raw)
        if isinstance(parsed, str):
            try:
                parsed = json.loads(parsed)
            except Exception:  # noqa: BLE001
                parsed = []
        errors = [str(e).strip() for e in parsed if isinstance(parsed, list) and e and str(e).strip()] if isinstance(parsed, list) else []
        if not errors:
            return "NO_ERRORS_FOUND: no validation-error markers detected on the page right now."
        return "FORM_ERRORS: " + " | ".join(errors)

    # Docstring below IS the tool description sent to the model on every
    # single call (see the StructuredTool.from_function registration a few
    # lines down: description=(self._request_human_help.__doc__ or
    # "").strip()) -- so it stays model-facing only. Implementation
    # pointers for future maintainers live here instead, as a plain
    # comment, not inside the docstring: the notify step is a separate
    # background poll -- see db.create_human_help_request and server.py's
    # _production_deepsearch_pause_loop.
    async def _request_human_help(self, reason: str) -> str:
        """Model-facing tool: call this when you recognize a login wall,
        CAPTCHA, 2FA prompt, or similar block you cannot get past yourself.

        Writes a durable request that texts the user, then waits, polling
        this tab for real activity: gives up after
        config.DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS with no activity
        at all; as long as fresh activity keeps appearing, extends the wait
        in config.DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS increments, capped
        regardless by config.DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS. A URL
        change is treated as resolved (logins/verifications almost always
        redirect on success). Never raises: returns a RESOLVED string with a
        fresh snapshot on success, or a BLOCKED string to give up on --
        continue with other independent work if there is any, otherwise
        wrap up and report that this task needs the user's help."""
        if self._user_id is None:
            return "ERROR: request_human_help isn't available outside a real user session."
        # No-op for a sub-worker (already eager); for the top-level provider,
        # covers the unlikely case this is genuinely the model's first-ever
        # action on a fresh run, before any browser_ tool call opened the
        # session -- see _ensure_live_session's own docstring.
        await self._ensure_live_session()

        console.system(f"Deepsearch: requesting human help on tab {self._tab_id} -- {reason}")
        request_row = await db.create_human_help_request(
            self._user_id, self._deepsearch_session_id, self._tab_id, reason,
        )
        request_id = request_row["id"] if request_row else None
        self._live_set_waiting(reason)

        try:
            start_fp = await self._page_fingerprint()
            start_url_hash = (start_fp // 1000) if start_fp is not None else None
            last_fp = start_fp
            elapsed = 0
            saw_any_activity = False
            deadline = config.DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS

            while elapsed < config.DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS:
                await asyncio.sleep(config.DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS)
                elapsed += config.DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS

                fp = await self._page_fingerprint()
                current_url_hash = (fp // 1000) if fp is not None else None

                if current_url_hash is not None and start_url_hash is not None and current_url_hash != start_url_hash:
                    if request_id is not None:
                        await db.resolve_human_help_request(request_id)
                    console.system(f"Deepsearch: human help on tab {self._tab_id} resolved after {elapsed}s (URL changed).")
                    snapshot_tool = self._raw_tools_by_name.get("browser_snapshot")
                    snapshot = None
                    if snapshot_tool is not None:
                        try:
                            snapshot = await snapshot_tool.coroutine()
                            self._state["snapshot_fresh"] = True
                        except Exception:  # noqa: BLE001
                            snapshot = None
                    return (
                        "RESOLVED: the page changed -- you're likely past the login/verification "
                        "now. Here is a fresh snapshot:\n\n"
                        + (str(snapshot) if snapshot is not None else "(snapshot unavailable, take one now)")
                    )

                if fp is not None and fp != last_fp:
                    saw_any_activity = True
                    deadline = elapsed + config.DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS
                last_fp = fp

                if elapsed >= deadline:
                    break

            if request_id is not None:
                await db.timeout_human_help_request(request_id)
            console.system(
                f"Deepsearch: human help on tab {self._tab_id} timed out after {elapsed}s "
                f"(saw_any_activity={saw_any_activity})."
            )
            return (
                "BLOCKED: no one finished the login/verification in time. If there is other "
                "independent work left to do, move on to that now. Otherwise, wrap up and "
                "report back honestly that this part needs the user's help."
            )
        finally:
            # Always clear the waiting flag on the way out, whichever branch
            # returned -- an early exception inside this method (a bug, an
            # unexpected None somewhere) must never leave the live-view page
            # stuck showing "waiting for you" past the point where this call
            # actually stopped waiting.
            self._live_clear_waiting()

    # Docstring below IS the tool description sent to the model -- see
    # request_human_help's own comment above for why implementation notes
    # live here as a plain comment instead. Manually gated (self._approval_gate
    # .confirm(...) called directly below) rather than going through
    # tools/common.py's trace_tool, matching how every other tool this file
    # adds directly to self.tools (request_human_help, delegate_website_task)
    # is wired -- none of them go through trace_tool, they're all plain
    # StructuredTools with whatever gating/checks they need inline.
    async def _generate_account_credential(self, site_name: str, username: str | None = None) -> str:
        """Model-facing tool: call this when a task requires CREATING a
        brand-new account on a site (not logging into one that already
        exists -- for that, use get_account_credential first). Generates a
        strong random password, stores it securely, and returns it to you
        ONCE so you can fill the signup form right now.

        site_name: a short, consistent slug for this site (e.g. 'airbnb',
        'united-mileageplus') -- use the same one later with
        get_account_credential to log back in.

        username: what to sign up with -- defaults to the user's own Messa
        email address if you don't pass one (recommended for most sites:
        it's an inbox Messa can read on the user's behalf, so verification
        emails and future correspondence from this account reach her
        automatically). Only pass your own value if the user asked for a
        specific username/email instead.

        CRITICAL: do not repeat the password this returns anywhere in your
        own reply or summary back to Messa/the user -- it's already stored
        securely. Just confirm the account was created."""
        if self._user_id is None:
            return "ERROR: generate_account_credential isn't available outside a real user session."
        resolved_username = username or self._messa_email
        if not resolved_username:
            return (
                "ERROR: no username to sign up with -- the user has no Messa email address "
                "provisioned yet, and none was given. Pass an explicit username, or ask the "
                "user what address/username to use."
            )

        if self._approval_gate is not None:
            allowed = await self._approval_gate.confirm(
                LABEL, "generate_account_credential", {"site_name": site_name, "username": resolved_username},
            )
        else:
            allowed = False
        if not allowed:
            msg = "BLOCKED: user declined to create a new account for this site."
            console.tool_result(LABEL, "generate_account_credential", msg)
            return msg

        password = credentials.generate_strong_password()
        try:
            encrypted = credentials.encrypt_secret(password)
        except credentials.CredentialsNotConfigured as e:
            return str(e)

        saved = await db.save_site_credential(self._user_id, site_name, resolved_username, encrypted)
        if saved is None:
            return (
                f"'{site_name}' already has a stored credential -- an account may already exist. "
                "Use get_account_credential instead of creating a new one, or pick a different "
                "site_name if this is genuinely a separate account."
            )
        console.system(f"Deepsearch: generated and stored a new credential for site {site_name!r}.")
        return (
            f"Generated and securely stored a new password for {site_name!r}. Use these EXACTLY "
            f"ONCE to fill the signup form -- username: {resolved_username}, password: {password}. "
            "Do NOT repeat this password in your own summary/report -- just confirm the account "
            "was created."
        )

    async def _get_account_credential(self, site_name: str) -> str:
        """Model-facing tool: call this BEFORE assuming a login wall needs
        request_human_help, if this might be an account Messa already
        created (via generate_account_credential) on an earlier task.
        Returns the stored username and password to fill the login form --
        or a plain 'nothing stored' message if there isn't one, in which
        case this is either a NEW account (use generate_account_credential)
        or an existing account whose password Messa never had in the first
        place (use request_human_help instead).

        CRITICAL: do not repeat the password this returns anywhere in your
        own reply or summary back to Messa/the user -- just confirm you
        logged in."""
        if self._user_id is None:
            return "ERROR: get_account_credential isn't available outside a real user session."
        row = await db.get_site_credential(self._user_id, site_name)
        if row is None:
            return (
                f"No stored credential for {site_name!r}. If you're signing up for the first "
                "time, use generate_account_credential. If this is an existing account Messa "
                "doesn't have the password for, use request_human_help instead."
            )
        try:
            password = credentials.decrypt_secret(row["encrypted_password"])
        except credentials.CredentialsNotConfigured as e:
            return str(e)
        except ValueError as e:
            return f"{e} Use request_human_help instead for this login."
        await db.mark_site_credential_used(self._user_id, site_name)
        return (
            f"Stored credential for {site_name!r} -- username: {row['username']}, "
            f"password: {password}. Use these to fill the login form now. Do NOT repeat the "
            "password in your own summary/report -- just confirm you logged in."
        )

    # Docstring below IS the tool description sent to the model on every
    # single call -- see the StructuredTool.from_function registration a
    # few lines down (description=(self._delegate_website_task.__doc__ or
    # "").strip()) -- so it stays model-facing only; implementation/test
    # pointers for future maintainers live here as a plain comment instead:
    # a new MCP client connection against a --shared-browser-context server
    # always lands on its own isolated tab (confirmed in
    # /tmp/test_mcp_multitab.py), and LangGraph genuinely runs multiple tool
    # calls from one AI turn concurrently (confirmed in
    # /tmp/test_agent_concurrency.py) -- see also the module docstring's
    # "Multi-site delegation" note.
    async def _delegate_website_task(self, url: str, instructions: str) -> str:
        """Model-facing tool: delegate independent work on ONE website to a
        fresh sub-worker with its OWN browser tab, in the SAME shared
        Browserbase session -- no new session, no extra billing unit.

        Call this MULTIPLE TIMES IN THE SAME TURN for genuinely independent
        multi-site work (e.g. "check the price on site A and on site B") --
        those calls run in parallel, not one after another. Up to
        config.DEEPSEARCH_MAX_SUBAGENTS sub-workers run at once; extra calls
        beyond that simply wait for a free slot rather than erroring, so you
        don't need to count concurrency yourself.

        This applies just as much to a plan's independent LEGS as to
        comparing the same thing across sites -- e.g. "flights from X to Y"
        and "hotels in Y" are two unrelated searches that don't need each
        other's result, so delegate both in the same turn (one call per
        leg: flights, hotels, a rental car, etc.) instead of working
        through them yourself one at a time. A trip- or plan-shaped request
        is exactly this case -- don't run it serially in your own tab just
        because it reads as "one task."

        Do NOT use this for a single site, or for a step that depends on
        another delegate_website_task call's result (e.g. "use the price
        from site A to decide what to search for on site B") -- handle
        those directly or one delegation at a time instead, since
        concurrent sub-workers can't see each other's progress until
        they've each returned.

        Returns the sub-worker's final summary, prefixed with the url it
        worked on. A sub-worker that hits a login wall calls
        request_human_help itself, on its own tab, exactly like the
        top-level agent would."""
        if not self._owns_server:
            return "ERROR: delegate_website_task cannot be called from within a sub-worker (no recursive delegation)."
        if self._model is None:
            return "ERROR: delegate_website_task isn't available in this context."
        # This is very plausibly the model's FIRST real action on a fresh
        # run (e.g. "check the price on site A and site B" as the opening
        # move) -- self._server_url doesn't exist yet until the top-level
        # session actually opens, so trigger that here, same as guarded()
        # does for a direct top-level tool call. A failure here means the
        # WHOLE session could never be opened at all -- every other tab
        # (the top-level's own, and any other concurrent delegation) is
        # equally doomed, so this is treated exactly like a mid-run dead
        # session: raised as _FatalBrowserSessionError so it propagates
        # past this method's own error handling below (see the matching
        # `except _FatalBrowserSessionError: raise` clauses) instead of
        # being reported as just this one site's failure.
        try:
            await self._ensure_live_session()
        except _FatalBrowserSessionError:
            raise
        except Exception as e:  # noqa: BLE001
            raise _FatalBrowserSessionError(str(e)) from e
        if not _domain_allowed(url):
            return f"BLOCKED: '{url}' is not in the allowed domain list."

        assert self._subagent_semaphore is not None  # set inside _ensure_live_session
        wait_start = time.monotonic()
        async with self._subagent_semaphore:
            queued_for = time.monotonic() - wait_start
            console.system(
                f"Deepsearch: delegating a sub-worker to {url}"
                + (f" (waited {queued_for:.2f}s for a free semaphore slot)" if queued_for > 0.05 else "")
                + "."
            )
            task_start = time.monotonic()
            try:
                async with BrowserToolProvider(
                    self._approval_gate, user_id=self._user_id,
                    deepsearch_session_id=self._deepsearch_session_id,
                    server_url=self._server_url, model=self._model,
                    messa_email=self._messa_email,
                ) as worker:
                    if self._user_id is not None:
                        live_activity.set_tab(self._user_id, worker._tab_id, url)
                    try:
                        # A pool of >1 key spreads concurrent sub-workers
                        # across separate OpenRouter keys (see
                        # config.SUBAGENT_API_KEY_POOL's own comment) --
                        # with the default pool of one, this is exactly
                        # self._model, unchanged. Timing callback tagged by
                        # tab id so a real run's logs show whether
                        # individual LLM calls slow down when several
                        # sub-workers run at once (see _TimingCallback's own
                        # docstring for how to read that).
                        sub_model = _pick_subagent_model(self._model)
                        _summarization = _deepsearch_summarization_middleware(sub_model)
                        sub_agent = create_agent(
                            model=sub_model, tools=worker.tools,
                            system_prompt=_SUBAGENT_SYSTEM_PROMPT.format(url=url),
                            middleware=[_summarization] if _summarization is not None else [],
                        )
                        try:
                            result = await asyncio.wait_for(
                                sub_agent.ainvoke(
                                    {"messages": [HumanMessage(
                                        content=f"Navigate to {url}, then: {instructions}"
                                    )]},
                                    config={
                                        "recursion_limit": config.DEEPSEARCH_SUBAGENT_MAX_STEPS,
                                        "callbacks": [_TimingCallback(worker._tab_id)],
                                    },
                                ),
                                timeout=config.DEEPSEARCH_MAX_SESSION_SECONDS,
                            )
                            summary = _last_ai_text(result["messages"])
                        except GraphRecursionError:
                            summary = "(hit its step limit before finishing -- partial progress only)"
                        except asyncio.TimeoutError:
                            summary = "(hit the overall session time limit before finishing)"
                        except _FatalBrowserSessionError:
                            # The WHOLE shared session is gone, not just this
                            # one site's task -- every other tab (the
                            # top-level agent's own, and any other
                            # concurrent sub-worker) is equally doomed, so
                            # this must NOT be swallowed into a normal
                            # per-site "(errored: ...)" result the way an
                            # ordinary failure is a few lines down. Re-raise
                            # past both this try and the outer one below so
                            # it reaches the top-level run and ends it --
                            # see _FatalBrowserSessionError's own docstring.
                            raise
                        except Exception as e:  # noqa: BLE001
                            console.tool_error(LABEL, "delegate_website_task", str(e))
                            summary = f"(errored: {e})"
                    finally:
                        if self._user_id is not None:
                            live_activity.clear_tab(self._user_id, worker._tab_id)
            except _FatalBrowserSessionError:
                raise  # see the matching except above -- must keep propagating, not be caught here.
            except Exception as e:  # noqa: BLE001
                # A failure opening the sub-worker's OWN tab/connection
                # (distinct from a failure during its task, handled above) --
                # still must never propagate as an unhandled exception into
                # the top-level agent's tool-calling loop; report it as a
                # normal (if unhappy) tool result instead.
                console.tool_error(LABEL, "delegate_website_task", str(e))
                return f"[{url}] ERROR: couldn't open a tab for this site: {e}"
            finally:
                console.system(f"Deepsearch: sub-worker for {url} finished in {time.monotonic() - task_start:.2f}s.")
        return f"[{url}] {summary}"

    def _guard(self, name: str, description: str, args_schema: Any) -> BaseTool:
        """Wraps ONE raw MCP tool by name/description/schema only -- not a
        live BaseTool instance -- so this can build a full, correctly-typed
        StructuredTool the model can see and call immediately, even before
        any real Browserbase session exists yet (see __aenter__'s
        cache-hit path and _ensure_live_session below). guarded() resolves
        the REAL underlying tool by name, fresh, on every call (via
        self._raw_tools_by_name[name]) rather than closing over one
        captured at wrap time -- the only way this can work whether
        self._raw_tools_by_name was populated just now (lazy open, first
        real call) or already, moments ago in __aenter__ (a cache-miss
        eager open, or any sub-worker, which is always eager -- see
        _ensure_live_session's own docstring for why only the TOP-LEVEL
        provider is ever lazy)."""
        state = self._state
        approval_gate = self._approval_gate

        async def _do(*args: Any, **kwargs: Any) -> Any:
            # The one lazy-open trigger point: the model's first real
            # browser tool call, whichever one it is, opens the actual
            # Browserbase session right here -- see _ensure_live_session's
            # own docstring for the full "why" (this is the fix for
            # error.txt's "browser sits open and billed through a 221s
            # blind planning call" finding). A no-op immediately once the
            # session is already open (checked first thing inside it), so
            # this costs nothing on the 2nd+ tool call of a run.
            await self._ensure_live_session()
            original = self._raw_tools_by_name[name]

            # Auto-resolve Set-of-Marks markers (e.g. target="[1]" or target="1") to stable selectors
            if "target" in kwargs and isinstance(kwargs["target"], str):
                kwargs["target"] = self._resolve_target(kwargs["target"])
            if name == "browser_fill_form" and "fields" in kwargs and isinstance(kwargs["fields"], list):
                for f in kwargs["fields"]:
                    if isinstance(f, dict) and "target" in f and isinstance(f["target"], str):
                        f["target"] = self._resolve_target(f["target"])

            # browser_snapshot EACCES fix: the model occasionally passes a
            # filename (e.g. "ubereats_signup_page_snapshot.md"), presumably
            # picked up from @playwright/mcp's own schema/examples, meaning
            # "write the snapshot to this file and tell me where." In
            # Docker/HF Spaces the working directory (/app) is read-only,
            # so that write fails with EACCES and the model gets no
            # snapshot at all -- effectively blind for that whole turn.
            # Redirecting to /tmp (always writable, and nothing here ever
            # reads the file back off disk -- the snapshot text itself
            # comes back in the tool result either way) fixes the write
            # without needing to strip the argument entirely, in case a
            # future @playwright/mcp version starts relying on the file
            # existing for something else. os.path.basename strips any
            # directory component the model included, so this can never
            # write outside /tmp regardless of what path shape it passes.
            if name == "browser_snapshot" and kwargs.get("filename"):
                safe_name = os.path.basename(str(kwargs["filename"])).strip() or "snapshot.md"
                kwargs["filename"] = f"/tmp/{safe_name}"

            # A real action is about to happen -- if the "reading" scroll
            # animation is still playing from the previous browser_snapshot,
            # tell it to wind down now, before this call's own action, so
            # the two don't visibly fight over the page (e.g. the reader
            # scrolling away from something a click is about to target).
            await self._stop_reading_animation()

            display_args = kwargs if kwargs else {"args": args}
            console.tool_call(LABEL, name, display_args)
            action_desc = _describe_action(name, display_args)
            self._live_set_description(action_desc)
            self._live_mark_active()

            if name == "browser_navigate":
                url = kwargs.get("url") or (args[0] if args else None)
                if url and not _domain_allowed(url):
                    msg = f"BLOCKED: '{url}' is not in the allowed domain list."
                    console.tool_result(LABEL, name, msg)
                    self._live_add_step(f"Blocked: {url} isn't an allowed domain")
                    return msg
                state["snapshot_fresh"] = False

            if name == "browser_snapshot":
                state["snapshot_fresh"] = True

            if (
                name in SNAPSHOT_DEPENDENT_TOOLS
                and _requires_fresh_snapshot(name, display_args)
                and not state["snapshot_fresh"]
            ):
                msg = (
                    "ERROR: You must call browser_snapshot before using an element ref like "
                    "'e12'. The page may have changed since your last snapshot. Take a fresh "
                    "snapshot now, then retry with a valid ref -- or use a stable selector "
                    "(role=..., text=..., a CSS selector) instead of a ref, which doesn't "
                    "require a fresh snapshot at all."
                )
                console.tool_result(LABEL, name, msg)
                return msg

            if name in DESTRUCTIVE_TOOLS:
                gate = approval_gate
                allowed = await gate.confirm(LABEL, name, display_args) if gate else False
                if not allowed:
                    msg = f"BLOCKED: user declined to run '{name}'."
                    console.tool_result(LABEL, name, msg)
                    self._live_add_step(f"Blocked: you declined \"{action_desc}\"")
                    return msg
                state["snapshot_fresh"] = False

            if name == "browser_type":
                text_val = str(kwargs.get("text", "") or (args[0] if args else ""))
                clean_text = self._sanitize_typed_text(text_val, kwargs.get("element"), self._current_url)
                if clean_text != text_val:
                    kwargs["text"] = clean_text
                    if args:
                        args = (clean_text, *args[1:])

            if name == "browser_fill_form":
                fields = kwargs.get("fields")
                if isinstance(fields, list):
                    for f in fields:
                        if isinstance(f, dict) and "value" in f:
                            f["value"] = self._sanitize_typed_text(str(f["value"]), f.get("name"), self._current_url)

            if name in CURSOR_ANIMATED_TOOLS:
                self._fire_cursor_move(name, kwargs.get("element"), kwargs.get("target"))

            try:
                result = await original.coroutine(*args, **kwargs)
                state["consecutive_errors"] = 0
                console.tool_result(LABEL, name, result)
                self._live_add_step(action_desc)
                if name == "browser_snapshot":
                    if isinstance(result, str):
                        url_m = re.search(r"Page URL:\s*([^\s\n]+)", result)
                        if url_m:
                            self._current_url = url_m.group(1).strip()
                    # Kept for the deterministic human-help backstop below
                    # (_looks_like_auth_wall) -- truncated so a very large
                    # page snapshot doesn't bloat this in-memory state.
                    state["last_snapshot_text"] = str(result)[:4000]
                    # The agent is about to spend some think-time deciding
                    # its next move against this snapshot's content -- fill
                    # that dead time on the live view instead of leaving the
                    # page sitting frozen. Fire-and-forget; see
                    # _start_reading_animation's own docstring for why this
                    # is never awaited here.
                    self._start_reading_animation()
                    # Deterministic snapshot-size backstop -- see
                    # config.DEEPSEARCH_LARGE_SNAPSHOT_CHARS/
                    # DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS's own comments. Only
                    # applies to a NON-depth-limited call: a depth= snapshot
                    # is already the disciplined choice, so a large result
                    # from one (an unusually wide shallow tree) isn't the
                    # behavior this is meant to correct.
                    if kwargs.get("depth") is None:
                        result = _apply_snapshot_size_backstop(result)
                if name == "browser_navigate":
                    nav_url = kwargs.get("url") or (args[0] if args else None)
                    if nav_url:
                        self._live_set_url(nav_url)
                    # No cursor-marker re-assert needed here (a real,
                    # separate-CDP-connection driver used to require one --
                    # see git history/README for that removed mechanism):
                    # the DOM-only cursor overlay is (re-)injected by
                    # @playwright/mcp's own --init-script on every fresh
                    # document load automatically, same as the reading
                    # animation and the click-ripple/typing-highlight/
                    # page-transition-flash scripts.
                return result
            except Exception as e:  # noqa: BLE001
                state["consecutive_errors"] += 1
                console.tool_error(LABEL, name, str(e))
                self._live_add_step(f"Error: {action_desc} failed ({e})")

                # Fix for error.txt's real production incident: a confirmed-
                # dead session (the user closed it, or Browserbase's own
                # timeout fired) used to come back here as an ordinary
                # retryable error -- "try a different approach" -- when
                # there IS no different approach; every other tool call on
                # this same session is doomed identically. That cost 11 more
                # full top-level LLM turns (~330s) in the real incident,
                # all guaranteed to fail. Raising here instead ends the run
                # immediately (see _run's own except Exception handler,
                # which already does the right cleanup/save-progress/report
                # honestly -- this is the ONLY change needed to reach it).
                if _is_dead_browser_session_error(e):
                    console.system(
                        f"Deepsearch: browser session is confirmed gone ({e}) -- ending this "
                        "run now instead of retrying against a session that can never come back."
                    )
                    raise _FatalBrowserSessionError(str(e)) from e

                # Softer, broader backstop: many failures in a row for ANY
                # reason (not necessarily this exact dead-session shape)
                # is still evidence of a genuinely stuck run -- see
                # config.DEEPSEARCH_MAX_CONSECUTIVE_TOOL_ERRORS's own
                # comment for why this is a separate, more generous check
                # than the auth-wall nudge just below.
                if state["consecutive_errors"] >= config.DEEPSEARCH_MAX_CONSECUTIVE_TOOL_ERRORS:
                    console.system(
                        f"Deepsearch: {state['consecutive_errors']} consecutive tool failures on "
                        f"{'the top-level tab' if self._owns_server else f'tab {self._tab_id}'} "
                        "-- ending this run now instead of continuing to retry."
                    )
                    raise _TooManyConsecutiveToolErrors(
                        f"{state['consecutive_errors']} consecutive tool failures "
                        f"(most recent: {e})"
                    ) from e

                msg = (
                    f"ERROR running '{name}': {e}. Do not retry with the exact same "
                    f"arguments. Take a fresh browser_snapshot, then try a different approach."
                )
                # Deterministic backstop (per your answer: model-driven
                # detection first, this is only the safety net): several
                # consecutive failures on a page that looks like a login/
                # verification wall is exactly the pattern of a model that
                # doesn't realize it's stuck retrying something a human
                # needs to do instead. Nudge it toward request_human_help
                # rather than letting it keep retrying indefinitely -- same
                # "one corrective nudge into the same graph thread" shape
                # already proven for executive_assistant's own quality
                # check, not a new mechanism.
                if state["consecutive_errors"] >= config.DEEPSEARCH_CAPTCHA_MAX_RETRIES and _looks_like_auth_wall(state["last_snapshot_text"]):
                    msg += (
                        f" This looks like it might be a login/verification/CAPTCHA page you can't get "
                        f"past on your own (limit of {config.DEEPSEARCH_CAPTCHA_MAX_RETRIES} attempts reached) -- "
                        "if so, call request_human_help with a short reason instead of retrying again."
                    )
                return msg

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            # self._tool_call_lock serializes every real call on THIS tab --
            # see its own __init__ comment for the full "why" (a production
            # crash: two browser_navigate calls dispatched concurrently by
            # ToolNode against the same tab raced each other and corrupted
            # the MCP client's shared anyio cancel scope, taking down the
            # whole webhook turn). Wrapping the entire _do() body, not just
            # the underlying original.coroutine(...) call, is deliberate --
            # _do() also mutates this tab's shared `state` dict (snapshot
            # freshness, consecutive-error count) and can await the
            # approval gate, both of which need the same serialization a
            # concurrent second call would otherwise race.
            async with self._tool_call_lock:
                return await _do(*args, **kwargs)

        return StructuredTool.from_function(
            name=name,
            description=description,
            args_schema=args_schema,
            coroutine=guarded,
        )


# A sub-worker's own system prompt (delegate_website_task) -- deliberately a
# trimmed copy of DEEPSEARCH_SYSTEM_PROMPT below, not the same string reused
# verbatim: a sub-worker is scoped to exactly ONE site and ONE goal handed to
# it by the top-level agent -- mirroring what deepsearch used to do
# sequentially per site before multi-site delegation existed, just
# parallelized, NOT a new, more open-ended kind of task -- so it doesn't
# need (and shouldn't be tempted to use) delegate_website_task or
# session-resumption guidance that only make sense for the orchestrator, and
# is explicitly told to stay narrow rather than explore.
_SUBAGENT_SYSTEM_PROMPT = (
    "You are a deepsearch sub-worker, delegated exactly ONE website and ONE goal: {url}. You "
    "have your OWN browser tab, separate from whoever delegated this and from any other "
    "sub-worker running at the same time -- stay on this one site; you can't see other tabs.\n"
    "- Do exactly what the instructions ask, nothing more -- no exploring beyond this one goal, "
    "no extra things to report. A short, focused session is what's wanted here.\n"
    "- This tab's session time is genuinely billed -- before navigating anywhere, ask whether "
    "read_webpage (or search_web) could answer it instead (comparing this site against others, "
    "confirming a fact, checking current content). Both cost no session time at all. Only use "
    "browser_navigate once you know THIS is the page you actually need to interact with.\n"
    "- After navigating, always call browser_snapshot to read actual page content. Prefer "
    "browser_find or browser_snapshot's `depth` argument over a full snapshot when you just "
    "need to confirm something worked.\n"
    "- browser_click/browser_type/browser_hover/browser_select_option/browser_drag accept "
    "EITHER an element ref from the most recent snapshot ('e12') OR a stable selector directly "
    "(e.g. 'role=button[name=\"Add to cart\"]', 'text=Submit', a CSS selector).\n"
    "- Base every factual claim strictly on text that literally appears in snapshots.\n"
    "- If a tool result starts with 'BLOCKED:' or 'ERROR', don't retry the same action -- take "
    "a fresh snapshot or try a different approach.\n"
    "- Hit an email verification/OTP screen right after signing up with generate_account_"
    "credential's DEFAULT username (the user's own Messa email)? Call "
    "await_email_verification_code, not request_human_help -- no human can read that inbox, "
    "so waiting for one to act would never resolve. Only fall back to request_human_help if it "
    "times out (the code may have gone to the user's own phone/personal email instead).\n"
    "- Hit a login wall, CAPTCHA, 2FA/one-time-code prompt, or any other block a real human "
    "would need to clear? First check get_account_credential(site_name) -- Messa may have "
    "already created this exact account for the user on an earlier task. Only call "
    "request_human_help if that comes back empty.\n"
    "- Need to CREATE a brand-new account (not log into one that exists)? Use "
    "generate_account_credential(site_name) for the password -- never invent one yourself. "
    "Use its returned username/password to fill the form immediately, then never repeat the "
    "password again in anything you say.\n"
    "- As soon as the goal is done, stop and reply with a clear, complete summary -- this goes "
    "straight back to whoever delegated this to you. You have a small, fixed step budget; "
    "don't spend it wandering.\n"
)

DEEPSEARCH_SYSTEM_PROMPT = (
    "You are deepsearch, the browser automation and research specialist. You perform web "
    "browsing tasks delegated to you by Messa, the orchestrator -- she only sends you tasks "
    "that genuinely need a real browser (clicking, forms, logins, carts, anything JS-rendered "
    "or interactive); a plain lookup she could answer with a quick search shouldn't reach you "
    "at all. If the message history already contains snapshots/navigation, you're continuing "
    "an earlier run, not starting over -- don't repeat completed steps.\n"
    "- Break the goal into steps. After navigating, always call browser_snapshot to read "
    "actual page content.\n"
    "- Money matters, not just speed: this whole session is billed by TIME held open, "
    "regardless of how much or little you actually do with it. Use the fast HTTP tools FIRST: "
    "search_web for searching and finding URLs, and read_webpage for reading page content. "
    "Both cost ZERO browser/session time and answer in <1-2 seconds. "
    "For a task that combines reading/comparing with acting (e.g. 'check these 3 sites and buy from "
    "whichever is cheapest'), the right shape is: search and read every candidate FIRST with "
    "search_web and read_webpage, decide which one wins, THEN spend real browser_navigate/session "
    "time only on that one -- never navigate to a page purely to compare it against others when a "
    "fast read would tell you the same thing.\n"
    "- Speed matters too -- users notice how long this takes. Prefer the cheaper tool for what "
    "you actually need right now instead of defaulting to a full browser_snapshot every time:\n"
    "  - browser_find(text=... or regex=...) locates one specific element (and its ref) "
    "without capturing the whole page -- use it when you know what you're looking for.\n"
    "  - browser_snapshot's own `depth` argument returns a shallower tree when you just need "
    "to confirm something worked, not the full page layout.\n"
    "  - browser_click/browser_type/browser_hover/browser_select_option/browser_drag accept "
    "EITHER an element ref from a snapshot ('e12') OR a stable selector directly (e.g. "
    "'role=button[name=\"Add to cart\"]', 'text=Submit', a CSS selector) -- reuse a selector "
    "for a repeat interaction instead of re-snapshotting.\n"
    "- Only use an element REF from the MOST RECENT snapshot -- an older one may point at the "
    "wrong element if the page changed. Selector-style targets don't have this problem; "
    "Playwright resolves them fresh every time.\n"
    "- Base every factual claim strictly on text that literally appears in snapshots.\n"
    "- ACT AS A HUMAN USER: You navigate pages visually with snapshots, mouse clicks, and typing. "
    "Never attempt to inspect script tags, webpack bundles, or reverse-engineer backend APIs.\n"
    "- FAST INTERACTION WITH SET-OF-MARKS: Call `browser_get_interactive_elements` to instantly see numbered markers [1], [2], [3] "
    "for all visible buttons, links, and form inputs without wading through a massive page snapshot. You can use these marker numbers "
    "directly as targets (e.g. target='[1]' or target='1') in `browser_click`, `browser_fill_form`, or `browser_action_chain`!\n"
    "- COMPOUND ACTIONS FOR HUMAN SPEED: When interacting with forms, dialogs, or multiple connected steps "
    "(e.g. accepting terms, clicking continue, and filling fields), use `browser_action_chain` to execute the whole sequence "
    "in ONE turn. This runs smoothly and quickly like a human user instead of taking 10 separate round-trips.\n"
    "- PREFER STABLE SELECTORS FOR REPEAT ACTIONS: Dynamic frameworks (Vue, React, Quasar) frequently change element refs "
    "(e.g. e12, e900) when modals open or animations play. Prefer stable selectors like `role=button[name='...']`, "
    "`role=checkbox`, `text='...'`, or CSS selectors (`button.q-btn`) which do not become stale when the page transitions.\n"
    "- FORM SUBMISSION: Modern single-page web apps (React, Next.js, Vue) frequently IGNORE the "
    "Enter key on input fields. NEVER rely on pressing Enter on an input field to submit a form. "
    "You MUST locate and click the visible submit or action button (e.g. button:has-text('Create Account'), "
    "button:has-text('Sign Up'), button[type='submit'], text='Submit', role=button[name='Create account']). "
    "Right after clicking it, call check_form_errors FIRST -- it's a small, fast, targeted check, cheaper "
    "than a full snapshot -- to see if the site rejected the submission. If it comes back NO_ERRORS_FOUND, "
    "take a fresh snapshot to confirm the page actually advanced (some sites neither error nor navigate "
    "visibly). If check_form_errors returns FORM_ERRORS, fix exactly what it describes and resubmit.\n"
    "- EARLY ESCALATION: If check_form_errors reports the SAME error twice in a row, or a form otherwise "
    "fails to submit after 2 attempts, or displays an unsolved CAPTCHA widget, do NOT loop or try to bypass "
    "it with scripts -- call request_human_help immediately so the user can assist.\n"
    "- If a tool result starts with 'BLOCKED:' or 'ERROR', don't retry the same action -- take "
    "a fresh snapshot or try a different approach.\n"
    "- Hit an email verification/OTP screen right after signing up with generate_account_"
    "credential's DEFAULT username (the user's own Messa email)? Call "
    "await_email_verification_code IMMEDIATELY AND STAY IN THIS TURN UNTIL IT RESOLVES, not "
    "request_human_help -- no human can read that inbox, so waiting for one to act would never "
    "resolve. Only fall back to request_human_help if it times out (the code may have gone to "
    "the user's own phone/personal email instead). Do NOT end your turn, reply with a summary, "
    "or hand this back to Messa while that screen is showing -- your browser session is torn "
    "down the moment you stop working, even if you never explicitly close it, which wipes the "
    "session and invalidates the very code you're waiting on. A later re-delegation with the "
    "code in hand would only open a fresh, already-expired session -- there is no recovering "
    "from that. Call await_email_verification_code yourself, wait for it to return the code, "
    "type it in, and finish the signup -- all in this same turn.\n"
    "- Hit a login wall, CAPTCHA, 2FA/one-time-code prompt, or any other block a real human "
    "would need to clear? First check get_account_credential(site_name) -- Messa may have "
    "already created this exact account for the user on an earlier task. Only call "
    "request_human_help (with a short reason) if that comes back empty -- see its own "
    "description for what happens next.\n"
    "- Need to CREATE a brand-new account (not log into one that exists)? Use "
    "generate_account_credential(site_name) for the password -- never invent one yourself. "
    "Use its returned username/password to fill the form immediately, then never repeat the "
    "password again in anything you say.\n"
    "- A request with SEVERAL INDEPENDENT websites or LEGS (e.g. comparing a price across "
    "three sites; flights + hotels + a rental car) should be decomposed and delegated -- one "
    "delegate_website_task call per site/leg, ALL IN THE SAME TURN, not worked through serially "
    "in your own tab just because it reads as one task. Only when the parts are genuinely "
    "independent, though: if one site's result determines the next step (e.g. which flight "
    "dates to search hotels for), handle those directly or one delegation at a time instead. "
    "See delegate_website_task's own description for the rest.\n"
    "- Don't know the right URL yet, or need factual background/links? Call search_web first -- "
    "it returns instant results and direct answers in <1s and costs ZERO browser/session time. "
    "NEVER navigate to google.com or any search engine in a browser tab; search_web answers it "
    "without opening a browser. Only use browser_navigate and browser actions when you genuinely "
    "need to interact: logging in, submitting forms, clicking buttons, or managing interactive flows.\n"
    "- When you're done, reply with a clear, complete, honest summary of what you found or did "
    "-- this goes straight back to the user.\n"
)


def build_deepsearch_subagent(
    user: UserContext, model: BaseChatModel, approval_gate: ApprovalGate | None = None
) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec. See module docstring for
    the open/close-per-task and session-resumption design."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        # v1 launch gate: live web browsing/deepsearch is OFF for the
        # general public, open only to admins and hand-picked beta accounts
        # -- see config.UserContext.has_deepsearch_access's own docstring
        # for the full "why" and for exactly how this flips to public later
        # (one env var, nothing here changes). Checked FIRST, before even
        # the usage-limit check right below -- an ungated user's request
        # doesn't touch the usage counter, doesn't open a Browserbase
        # session, doesn't spawn the MCP subprocess, doesn't run an LLM
        # turn. Plain, calm decline (not an "ERROR:"-prefixed string) since
        # this is an expected, everyday outcome for most users right now,
        # not a failure -- registry.py's system prompt already primes
        # Messa on how to phrase this to someone who asks.
        if not user.has_deepsearch_access:
            console.system(
                f"Deepsearch: declined for user #{user.user_id} -- browsing/deepsearch isn't "
                "enabled on this account yet (beta-gated for v1)."
            )
            return {"messages": [AIMessage(content=(
                "Live web browsing/research isn't available on your account yet -- it's in a "
                "limited beta right now while we get it fully ready for everyone. I can still "
                "help with everything else!"
            ))]}

        # Per-USER concurrent-session cap (faster-than-human.md's "cost
        # controls" upgrade -- config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_
        # USER, layered UNDER the global cap a few lines down, which only
        # bounds the process-wide total). Checked before even the
        # usage-limits consumption below -- deepsearch_control.active_count
        # is a pure in-memory read (no DB round trip, no model call), so
        # it's the cheapest possible thing to check first, and a user who's
        # already at their own cap shouldn't have a usage-limits unit
        # consumed for a request that's about to be declined anyway. Reads
        # the count of THIS user's own already-registered tasks -- this
        # request hasn't called deepsearch_control.register() yet at this
        # point (that happens further down), so the count here reflects
        # only genuinely concurrent OTHER requests, not this one.
        if deepsearch_control.active_count(user.user_id) >= config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER:
            console.system(
                f"Deepsearch: declined for user #{user.user_id} -- already at their own "
                f"concurrent-session cap ({config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER})."
            )
            return {"messages": [AIMessage(content=(
                "You've already got a browsing session or two running -- let one of those finish "
                "(or tell me to stop it) before starting another."
            ))]}

        # One usage-limits unit per top-level browsing session opened here
        # (session resumption included -- BrowserToolProvider's own
        # "open/close-per-task" design means even a continuation opens a
        # fresh Browserbase session, so it costs the same real money as a
        # brand-new task and is metered the same way). Checked as the very
        # first thing in this function, before the session lookup or
        # anything else that costs a DB round trip or a model call -- a
        # blocked request never opens a Browserbase session, never spawns
        # the MCP server subprocess, and never runs an LLM turn, which is
        # both the cheapest and the fastest way to enforce this.
        limit_result = await usage.check_and_consume(user, usage.FEATURE_BROWSE_ACTIONS)
        if not limit_result.allowed:
            return {"messages": [AIMessage(content=limit_result.upgrade_message)]}

        # Global concurrent-session cap (config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS
        # -- see _get_session_semaphore's own comment for why this exists at
        # all). A BOUNDED wait, not an unbounded queue: at launch-scale
        # traffic, a request that sits behind an indefinite backlog is worse
        # for someone texting than a prompt "try again shortly" reply, and an
        # unbounded queue of waiting coroutines is itself a resource leak
        # under sustained overload. Acquired here, before the session
        # lookup/creation below and before deepsearch_control.register --
        # a request that can't get a slot never touches the DB for a new
        # session row and never shows up as an "active search" the model or
        # cancel_active_search would need to know about. Released in the
        # `finally` alongside deepsearch_control.unregister() further down,
        # once this run (successful, errored, timed out, or cancelled) is
        # actually done with its Browserbase session and MCP subprocess.
        semaphore = _get_session_semaphore()
        try:
            await asyncio.wait_for(
                semaphore.acquire(), timeout=config.DEEPSEARCH_SESSION_QUEUE_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            console.system(
                f"Deepsearch: at capacity ({config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS} concurrent "
                f"sessions) -- user #{user.user_id}'s request timed out waiting "
                f"{config.DEEPSEARCH_SESSION_QUEUE_WAIT_SECONDS}s for a free slot."
            )
            return {
                "messages": [AIMessage(content=(
                    "Deepsearch is at capacity right now -- a lot of browsing sessions are "
                    "running at once. Please try again in a minute or two."
                ))]
            }

        incoming = state["messages"]
        last_text = _message_text(incoming[-1]) if incoming else ""
        match = _SESSION_REF_RE.search(last_text)

        session_row = None
        if match:
            session_row = await db.get_deepsearch_session(user.user_id, int(match.group(1)))

        initial_resume_url = None
        if session_row:
            session_id = session_row["id"]
            task_title = session_row["title"]
            try:
                prior_messages = sanitize_tool_messages(messages_from_dict(json.loads(session_row["messages"])))
            except Exception:
                prior_messages = []
            initial_resume_url = _extract_last_target_url(prior_messages)
            remainder = _SESSION_REF_RE.sub("", last_text).strip(" .:-\n")
            notice = f" (The browser was automatically reopened and navigated back to {initial_resume_url})." if initial_resume_url else ""
            follow_up = HumanMessage(
                content=f"Continuing this task.{notice} Additional instruction: {remainder}"
                if remainder else f"Continue where you left off.{notice}"
            )
            messages = [*prior_messages, follow_up]
            steps_so_far = session_row["steps_used"]
            console.system(
                f"Deepsearch: resuming session #{session_id} ({len(prior_messages)} prior messages"
                f"{f', auto-reopening {initial_resume_url}' if initial_resume_url else ''})."
            )
        else:
            messages = list(incoming)
            task_title = (last_text.strip() or "Deepsearch task")[:255]
            created = await db.create_deepsearch_session(user.user_id, task_title)
            session_id = created["id"] if created else None
            steps_so_far = 0
            if session_id:
                console.system(f"Deepsearch: started session #{session_id}.")

        checkpointer = MemorySaver()
        run_config = {
            "configurable": {"thread_id": f"deepsearch-{session_id or 'untracked'}"},
            "recursion_limit": config.DEEPSEARCH_MAX_STEPS,
            # Per-LLM-call latency logging -- see _TimingCallback's own
            # docstring for what this is actually diagnosing (the CTO
            # discussion's "why isn't parallel delegation as fast as
            # expected" question).
            "callbacks": [_TimingCallback("top-level")],
        }
        run_start = time.monotonic()

        # Registers THIS task (asyncio.current_task(), resolved inside
        # register() itself) as "the deepsearch run currently in flight for
        # this user" -- see deepsearch_control's own module docstring for
        # why this works at all (no per-user message queue -- a later text
        # from the same user runs as its own concurrent turn and can find
        # this) and agents/registry.py's cancel_active_search tool +
        # _build_system_prompt's "active search" hint for the model-driven
        # trigger. Registered before anything that could take real time
        # (the browser hasn't even opened yet, lazily or otherwise) so a
        # "stop" text sent even during a long initial planning call can
        # still find and cancel this. The whole rest of this function runs
        # inside the try below purely so unregister() below is guaranteed
        # to fire on every exit path, cancellation included.
        deepsearch_control.register(user.user_id, task_title)
        try:
            # live_activity.start() is called HERE, before BrowserToolProvider
            # even opens -- not after, like it used to be. This is the actual
            # fix for the real "only 1 tile ever shows, no matter how many tabs
            # are really open" bug: BrowserToolProvider (below) creates the
            # Browserbase session and immediately calls live_activity.set_session_id()
            # to record bb_session_id, which server.py's _build_live_tiles needs to
            # look up the real pages[] and build one tile per tab. start()
            # unconditionally REPLACES this user's whole live_activity entry
            # with a fresh dict (see its own docstring/module comment) -- when it
            # ran AFTER the session opened, it was silently wiping the
            # bb_session_id that set_session_id had just written moments
            # earlier, every single time. With bb_session_id always back to None
            # by the time anyone polled /live/<token>/status, _build_live_tiles
            # always took its no-bb_session_id fallback path -- exactly one
            # tile, built from the session-level live_view_url, regardless of
            # how many tabs Browserbase actually had open. Starting it first
            # means the later set_session_id call (which uses
            # _state.setdefault, not a fresh dict) lands on top of this same
            # entry instead of being overwritten by it.
            live_activity.start(user.user_id, task_title)

            # Constructed as a plain variable (not inline in the `async
            # with`) specifically so provider.live_view_url is always a
            # valid thing to read in the outer `finally` below, whether or
            # not __aenter__/the session ever actually opened -- see that
            # finally's own comment. task_title is threaded through so
            # BrowserToolProvider._ensure_live_session can call
            # db.set_live_browser_active the moment the session actually
            # opens (possibly well after this point now that opening is
            # lazy -- see that method's own docstring), instead of this
            # function doing it eagerly right after __aenter__ like before.
            # Engine routing: Stagehand v4 (macro-actions) or Playwright MCP (micro-actions)
            _use_stagehand = config.DEEPSEARCH_ENGINE == "stagehand"

            if _use_stagehand:
                # Stagehand v4: no warm-session cache (no subprocess to preserve)
                provider = StagehandToolProvider(
                    approval_gate, user_id=user.user_id, deepsearch_session_id=session_id,
                    model=model,
                    messa_email=user.messa_email, user_email=user.email, task_title=task_title,
                    phone_number=user.phone_number, live_view_share_url=user.live_view_share_url,
                    initial_resume_url=initial_resume_url,
                )
            else:
                # Playwright MCP: warm-session cache (preserves subprocess + tab state)
                await _cleanup_expired_warm_sessions()
                provider = None
                if session_id and session_id in _WARM_SESSION_PROVIDERS:
                    candidate = _WARM_SESSION_PROVIDERS.get(session_id)
                    if (
                        candidate is not None
                        and candidate._mcp_proc is not None
                        and candidate._mcp_proc.returncode is None
                    ):
                        console.system(
                            f"Deepsearch: REUSING active warm browser session #{session_id} "
                            f"(preserving live tab, modal state, and form inputs!)."
                        )
                        provider = candidate
                        provider._approval_gate = approval_gate
                        provider._is_warm = False
                        _WARM_SESSION_PROVIDERS.pop(session_id, None)
                        _WARM_SESSION_EXPIRY.pop(session_id, None)
                    else:
                        _WARM_SESSION_PROVIDERS.pop(session_id, None)
                        _WARM_SESSION_EXPIRY.pop(session_id, None)

                if provider is None:
                    provider = BrowserToolProvider(
                        approval_gate, user_id=user.user_id, deepsearch_session_id=session_id, model=model,
                        messa_email=user.messa_email, user_email=user.email, task_title=task_title,
                        phone_number=user.phone_number, live_view_share_url=user.live_view_share_url,
                        initial_resume_url=initial_resume_url,
                    )

            try:
                async with provider:
                    try:
                        _summarization = _deepsearch_summarization_middleware(model)
                        user_identity_prompt = (
                            f"\n\nUSER IDENTITY & AUTHORIZED SIGNUP CREDENTIALS:\n"
                            f"- Target User Name: {user.name or 'User'}\n"
                            f"- User Messa Email: {user.messa_email or 'None provisioned'}\n"
                            f"- User Personal Email: {user.email or 'None'}\n"
                            f"STRICT SIGNUP RULE: When creating an account, signing up, or registering on ANY website or service, "
                            f"you MUST use the user's authorized Messa email address ({user.messa_email}). "
                            f"NEVER invent, guess, or type fictional/dummy email addresses (such as dummy @gmail.com or @space addresses). "
                            f"Any signup with a made-up address will fail verification."
                        )
                        _active_system_prompt = (
                            STAGEHAND_SYSTEM_PROMPT + user_identity_prompt
                            if _use_stagehand
                            else DEEPSEARCH_SYSTEM_PROMPT + user_identity_prompt
                        ) + reliability.RELIABILITY_GUARDRAIL_STR
                        # Active Task Scratchpad + Skills Playbook (see
                        # tools/scratchpad_tools.py's own module docstring)
                        # -- this is the flagship use case the feature was
                        # designed around: a site-navigation lesson learned
                        # on one Amazon run (a selector, a checkout quirk)
                        # persists and speeds up every future deepsearch
                        # run against amazon.com, for every user, without
                        # re-deriving it from the model each time. Attached
                        # here rather than by registry.py since this whole
                        # subagent is a CompiledSubAgent that builds its own
                        # inner agent fresh on every delegation.
                        _deepsearch_tools = provider.tools
                        if config.SCRATCHPAD_AND_SKILLS_ENABLED:
                            _deepsearch_tools = _deepsearch_tools + build_scratchpad_tools(
                                user, "deepsearch", approval_gate
                            )
                            _active_system_prompt = _active_system_prompt + await scratchpad_prompt_block(user, "deepsearch")
                        # Zero-delta circuit breaker (docs/smart_autonomous_
                        # agent_architecture.md, System 6) -- only for the
                        # Stagehand engine, which had no general breaker of
                        # any kind before this (the legacy Playwright-MCP
                        # engine's own consecutive_errors counter lives
                        # inside BrowserToolProvider's tool-wrapping loop
                        # instead, further up this file). Listed first so it
                        # sees the raw tool result before _summarization can
                        # truncate/compact it (middleware compose outermost
                        # -> innermost in list order).
                        _run_middleware: list[Any] = []
                        if _use_stagehand:
                            _run_middleware.append(StagehandZeroDeltaMiddleware(provider))
                        if _summarization is not None:
                            _run_middleware.append(_summarization)
                        inner_agent = create_agent(
                            model=model, tools=_deepsearch_tools,
                            system_prompt=_active_system_prompt,
                            checkpointer=checkpointer,
                            middleware=_run_middleware,
                        )
                        status = "completed"
                        # Set True only by the anti-hallucination gate below,
                        # never by the step-limit (GraphRecursionError) path
                        # further down -- distinguishes the two ways this run
                        # can end up "active" so the header text below never
                        # claims a warm, resumable browser session for a run
                        # that in fact never opened one (no tool call ever
                        # happened) or where GraphRecursionError already
                        # decided independently whether the session is warm.
                        gate_forced_active = False
                        try:
                            result = await asyncio.wait_for(
                                inner_agent.ainvoke({"messages": messages}, config=run_config),
                                timeout=config.DEEPSEARCH_MAX_SESSION_SECONDS,
                            )
                            final_messages = result["messages"]

                            # Anti-hallucination completion gate: a reasoning model can
                            # occasionally produce a plausible "done" summary without ever
                            # calling a single tool -- no browser action, no search_web/
                            # read_webpage call, nothing that could have produced real
                            # information -- and this success path would otherwise trust
                            # that completely, reporting fabricated completion back to the
                            # user. The signal is whether THIS RUN's own new messages
                            # contain even one tool call, anywhere -- deliberately not
                            # provider._live_session_ready alone, since a task legitimately
                            # answered entirely via search_web/read_webpage (zero browser
                            # actions, by design -- see the "cost ZERO browser/session
                            # time" system-prompt guidance above) is a perfectly real
                            # result and must not trip this.
                            new_messages = final_messages[len(messages):]
                            had_any_tool_call = any(getattr(m, "tool_calls", None) for m in new_messages)
                            if not had_any_tool_call:
                                console.system(
                                    f"Deepsearch: session #{session_id} reported completion "
                                    "without ever calling a tool -- giving it one chance to "
                                    "actually execute before trusting that."
                                )
                                try:
                                    retry_result = await asyncio.wait_for(
                                        inner_agent.ainvoke(
                                            {"messages": [HumanMessage(content=(
                                                "You reported this task as done without calling "
                                                "any tool -- no browser action, no search, no page "
                                                "read happened. That can't be a real result. Either "
                                                "actually execute the tools needed to complete this "
                                                "task now, or tell me honestly that you were not "
                                                "able to do it and why."
                                            ))]},
                                            config=run_config,
                                        ),
                                        timeout=config.DEEPSEARCH_MAX_SESSION_SECONDS,
                                    )
                                    retry_messages = retry_result["messages"]
                                    retry_new = retry_messages[len(final_messages):]
                                    final_messages = retry_messages
                                    if not any(getattr(m, "tool_calls", None) for m in retry_new):
                                        status = "active"
                                        gate_forced_active = True
                                        console.system(
                                            f"Deepsearch: session #{session_id} still called no "
                                            "tool after one corrective nudge -- marking honestly "
                                            "instead of trusting the claim."
                                        )
                                except (asyncio.TimeoutError, GraphRecursionError):
                                    status = "active"
                                    gate_forced_active = True
                                    console.system(
                                        f"Deepsearch: session #{session_id}'s corrective nudge "
                                        "itself hit a limit -- marking honestly instead of "
                                        "trusting the original claim."
                                    )
                                # Any OTHER exception here (a confirmed-dead session, too many
                                # consecutive tool errors, cancellation) is deliberately not
                                # caught -- it propagates to the exact same except blocks below
                                # that already handle it for the original call, which still
                                # ends this run honestly (never as a fabricated "completed").
                            elif not any(
                                isinstance(tc, dict) and tc.get("name") == "await_email_verification_code"
                                for m in new_messages
                                for tc in (getattr(m, "tool_calls", None) or [])
                            ) and _looks_like_pending_otp(
                                getattr(final_messages[-1], "content", None) if final_messages else None
                            ):
                                # agent-feedback.md item 2 -- OTP early-return backstop. This
                                # run DID call tools (had_any_tool_call is True, so the
                                # anti-hallucination gate above didn't fire), but its own final
                                # message reads like it's sitting on an OTP/verification screen
                                # and it never called await_email_verification_code. Ending the
                                # turn here would fall through to "Finished successfully" below,
                                # exit `async with provider:`, and tear down the browser --
                                # invalidating the very code a later re-delegation would need.
                                # Same "one corrective nudge" shape as the hallucination gate
                                # above, just a different trigger signal.
                                console.system(
                                    f"Deepsearch: session #{session_id} appears to be ending its "
                                    "turn on an OTP/verification screen without calling "
                                    "await_email_verification_code -- nudging it to stay and wait "
                                    "instead of letting the browser close underneath it."
                                )
                                try:
                                    retry_result = await asyncio.wait_for(
                                        inner_agent.ainvoke(
                                            {"messages": [HumanMessage(content=(
                                                "Your last message reads like you're on an email "
                                                "verification/OTP screen, but you never called "
                                                "await_email_verification_code. Do NOT end this turn "
                                                "or hand this back -- your browser session closes the "
                                                "moment you stop, and that will invalidate the code. "
                                                "Call await_email_verification_code right now and wait "
                                                "for it, finish entering the code, or explain honestly "
                                                "why that tool doesn't apply to what's on screen."
                                            ))]},
                                            config=run_config,
                                        ),
                                        timeout=config.DEEPSEARCH_MAX_SESSION_SECONDS,
                                    )
                                    final_messages = retry_result["messages"]
                                except (asyncio.TimeoutError, GraphRecursionError):
                                    status = "active"
                                    gate_forced_active = True
                                    console.system(
                                        f"Deepsearch: session #{session_id}'s OTP nudge itself hit "
                                        "a limit -- marking active honestly rather than closing the "
                                        "browser on what may still be a live verification wait."
                                    )
                                # Any OTHER exception propagates to the same except blocks below
                                # that already handle a dead session/too-many-errors/cancellation
                                # honestly -- never a silent crash, same as the gate above.

                            # Finished successfully -- release warm session if previously cached
                            if session_id and session_id in _WARM_SESSION_PROVIDERS:
                                old = _WARM_SESSION_PROVIDERS.pop(session_id, None)
                                _WARM_SESSION_EXPIRY.pop(session_id, None)
                                if old is not None:
                                    old._is_warm = False
                                    await old.close()
                        except GraphRecursionError:
                            status = "active"
                            state_snapshot = await inner_agent.aget_state(run_config)
                            final_messages = state_snapshot.values.get("messages", messages)
                            console.system(
                                f"Deepsearch: hit its {config.DEEPSEARCH_MAX_STEPS}-step limit -- "
                                f"saving progress to session #{session_id} for later."
                            )
                            # Keep provider warm in memory so continuing retains form inputs and modal state
                            # (Playwright MCP only -- Stagehand has no subprocess to preserve)
                            if (
                                not _use_stagehand
                                and session_id
                                and getattr(provider, "_live_session_ready", False)
                                and getattr(provider, "_mcp_proc", None) is not None
                                and provider._mcp_proc.returncode is None
                            ):
                                provider._is_warm = True
                                _WARM_SESSION_PROVIDERS[session_id] = provider
                                _WARM_SESSION_EXPIRY[session_id] = time.monotonic() + WARM_SESSION_TTL_SECONDS
                                console.system(
                                    f"Deepsearch: kept browser session #{session_id} WARM in memory (TTL={int(WARM_SESSION_TTL_SECONDS)}s)."
                                )
                        except asyncio.TimeoutError:
                            status = "active"
                            try:
                                state_snapshot = await inner_agent.aget_state(run_config)
                                final_messages = state_snapshot.values.get("messages", messages)
                            except Exception:  # noqa: BLE001
                                final_messages = messages
                            console.system(
                                f"Deepsearch: hit its {config.DEEPSEARCH_MAX_SESSION_SECONDS}s overall "
                                f"session time limit -- closing the browser and saving progress to "
                                f"session #{session_id} for later."
                            )
                            if session_id in _WARM_SESSION_PROVIDERS:
                                old = _WARM_SESSION_PROVIDERS.pop(session_id, None)
                                _WARM_SESSION_EXPIRY.pop(session_id, None)
                                if old is not None:
                                    old._is_warm = False
                                    await old.close()
                        except asyncio.CancelledError:
                            status = "abandoned"
                            console.system(
                                f"Deepsearch: session #{session_id} cancelled by the user."
                            )
                            if session_id in _WARM_SESSION_PROVIDERS:
                                old = _WARM_SESSION_PROVIDERS.pop(session_id, None)
                                _WARM_SESSION_EXPIRY.pop(session_id, None)
                                if old is not None:
                                    old._is_warm = False
                                    await old.close()
                            try:
                                state_snapshot = await inner_agent.aget_state(run_config)
                                final_messages = state_snapshot.values.get("messages", messages)
                            except Exception:  # noqa: BLE001
                                final_messages = messages
                            if session_id:
                                try:
                                    await db.update_deepsearch_session(
                                        session_id,
                                        messages_json=json.dumps(messages_to_dict(sanitize_tool_messages(final_messages)), default=str),
                                        status=status,
                                        summary="(cancelled by the user)",
                                        steps_used=steps_so_far + len(final_messages),
                                        live_view_url=provider.live_view_url,
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                            raise
                        except Exception as e:  # noqa: BLE001
                            status = "active"
                            console.tool_error(LABEL, "deepsearch", str(e))
                            try:
                                state_snapshot = await inner_agent.aget_state(run_config)
                                final_messages = state_snapshot.values.get("messages", messages)
                            except Exception:
                                final_messages = messages
                            final_messages = [*final_messages, AIMessage(content=f"(run errored: {e})")]
                    finally:
                        if provider.live_view_url and not getattr(provider, "_is_warm", False):
                            live_activity.set_closing(user.user_id)
            finally:
                if provider.live_view_url and not getattr(provider, "_is_warm", False):
                    await db.clear_live_browser_active(user.user_id)
                if not getattr(provider, "_is_warm", False):
                    live_activity.clear(user.user_id)

            console.system(f"Deepsearch: whole run took {time.monotonic() - run_start:.2f}s (status={status}).")
            summary = _last_ai_text(final_messages)

            if session_id:
                await db.update_deepsearch_session(
                    session_id,
                    messages_json=json.dumps(messages_to_dict(sanitize_tool_messages(final_messages)), default=str),
                    status=status,
                    summary=summary,
                    steps_used=steps_so_far + len(final_messages),
                    live_view_url=provider.live_view_url,
                )
                if status == "active" and gate_forced_active:
                    # The anti-hallucination gate, not the step limit,
                    # forced this -- no browser session was necessarily ever
                    # even opened (a run can reach this having called zero
                    # tools at all), so "browser kept warm" would be a false
                    # claim here. Still session-resumable either way (the
                    # resume path re-derives a fresh session regardless --
                    # see _run's own "open/close-per-task" resume handling
                    # above), just without promising continuity that may
                    # not exist.
                    header = (
                        f"[deepsearch session #{session_id} -- could not confirm this was actually "
                        f"done (no tool/browser action happened). Say \"continue session #{session_id}\" "
                        "to have it try again.]\n"
                    )
                elif status == "active":
                    header = (
                        f"[deepsearch session #{session_id} -- paused at step limit, browser kept warm. "
                        f"Say \"continue session #{session_id}\" to keep going.]\n"
                    )
                else:
                    header = f"[deepsearch session #{session_id} -- completed]\n"
            else:
                header = "[deepsearch -- session tracking not enabled: run migrations/004_deepsearch_sessions.sql]\n"

            return {"messages": [AIMessage(content=header + summary)]}
        finally:
            deepsearch_control.unregister(user.user_id)
            semaphore.release()

    return {
        "name": "deepsearch",
        "description": (
            "Performs live web browsing and research: navigating sites, reading pages, "
            "filling forms, clicking through flows, and reporting back what it found/did. "
            "Use for anything that requires actually visiting a website. Opens and fully "
            "closes its own browser per request. Long research tasks may hit a step limit "
            "before finishing -- when that happens the reply says so and gives a session id; "
            "include \"session #<id>\" in a follow-up delegation's description to resume "
            "exactly where it left off instead of starting over.\n"
            "Do NOT use for 3rd-party apps/platforms (Reddit, Slack, Todoist, Notion, Google "
            "Calendar, and more) that integrations_agent can reach via Composio's authenticated "
            "API -- that's faster and more reliable than browsing the site by hand. Only use "
            "for one of these once integrations_agent itself has reported the app or action "
            "isn't supported there."
        ),
        "runnable": RunnableLambda(_run),
    }
