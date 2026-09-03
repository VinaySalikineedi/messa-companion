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
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

from .. import config, console, credentials, db, live_activity, usage
from ..approval import ApprovalGate
from ..channels import browserbase
from ..channels.browserbase import BrowserbaseError
from ..config import UserContext
from .jina_reader import fetch_rendered_page_text as _jina_fetch_rendered_page_text
from .parallel_search import parallel_web_fetch as _parallel_web_fetch

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

_SESSION_REF_RE = re.compile(r"session\s*#?\s*(\d+)", re.IGNORECASE)

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
    ):
        self._approval_gate = approval_gate
        self._user_id = user_id
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
        if (
            config.DEEPSEARCH_CURSOR_OVERLAY
            or config.DEEPSEARCH_READING_ANIMATION
            or config.DEEPSEARCH_CLICK_RIPPLE
            or config.DEEPSEARCH_TYPING_HIGHLIGHT
            or config.DEEPSEARCH_PAGE_TRANSITION_FLASH
        ):
            mcp_args += ["--init-script", str(_CURSOR_OVERLAY_SCRIPT_PATH)]

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
        if self._owns_server:
            # One Browserbase Context per user, created once and reused
            # forever -- this is what makes logins survive between tasks
            # (and, unlike the old --user-data-dir profile directory,
            # survives an HF Space restart too, since it isn't on the
            # container's disk at all).
            # This whole method used to have no top-level error handling
            # (same as the original local-Chromium version) -- fine when the
            # only failure mode was "npx/chromium missing," which the
            # guarded tool-call path below already logs clearly. Now that
            # opening a browser means two network round trips to Browserbase
            # before anything else runs (auth, quota, an expired/malformed
            # key, HF's own egress to api.browserbase.com being blocked --
            # all plausible, all silent without this), a failure here needs
            # its own clear, findable log line instead of surfacing as an
            # unlabeled exception three layers up. Two separate try/excepts
            # so the log line itself tells you whether it was Browserbase or
            # the local MCP/CDP connection step that failed.
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
                    # Lets server.py's status route re-poll this session's
                    # /debug endpoint later to resolve a specific tab's own
                    # live-view url (see live_activity.set_session_id).
                    live_activity.set_session_id(self._user_id, self._bb_session_id)
            except Exception as e:  # noqa: BLE001
                console.tool_error(LABEL, "browserbase_session_create", str(e))
                raise

            # Best-effort: not having a live-view link yet shouldn't block the run.
            try:
                self.live_view_url = await browserbase.get_live_view_url(self._bb_session_id)
            except BrowserbaseError as e:
                console.tool_error(LABEL, "browserbase_live_view", str(e))

            try:
                self._server_url = await self._spawn_mcp_http_server(connect_url)
                self._subagent_semaphore = asyncio.Semaphore(config.DEEPSEARCH_MAX_SUBAGENTS)
            except Exception as e:  # noqa: BLE001
                console.tool_error(LABEL, "browserbase_cdp_connect", str(e))
                # __aexit__ is NOT called by `async with` when __aenter__
                # itself raises -- without this, a Browserbase session that
                # was created just above but never got a working CDP
                # connection would leak (left running until it idles out on
                # Browserbase's side, rather than released immediately).
                # Left unfixed, repeated failed attempts would each leak
                # another session -- on the free plan's low concurrent-
                # session cap, that alone could make every *subsequent*
                # attempt fail at browserbase_session_create with a
                # quota/limit error, which would look identical to this
                # failure from the outside. Best-effort: we're already
                # failing, a second error here shouldn't mask the first.
                if self._mcp_proc is not None and self._mcp_proc.returncode is None:
                    self._mcp_proc.terminate()
                if self._bb_session_id is not None:
                    try:
                        await browserbase.release_session(self._bb_session_id)
                    except Exception as release_err:  # noqa: BLE001
                        console.tool_error(LABEL, "browserbase_release_after_failure", str(release_err))
                raise

        try:
            # Both the owning provider (connecting to the server it just
            # spawned above) and a sub-worker (connecting to an
            # already-running server passed in via server_url) land here the
            # same way -- a brand-new, independent streamable-HTTP client
            # connection, which is what gives each one its own isolated tab
            # within the shared browser context (confirmed empirically, see
            # /tmp/test_mcp_multitab.py).
            self._client = MultiServerMCPClient({
                "playwright": {"url": self._server_url, "transport": "streamable_http"}
            })
            self._session_cm = self._client.session("playwright")
            self._session = await self._session_cm.__aenter__()
            raw_tools = await load_mcp_tools(self._session)
            self._raw_tools_by_name = {t.name: t for t in raw_tools}
            if not self._owns_server:
                # Claim our OWN tab within the shared default browser
                # context, replacing what the old --isolated flag used to
                # guarantee (see _spawn_mcp_http_server's comment and the
                # module docstring's "Multi-site delegation" section).
                # Deliberately raised (not swallowed) on failure -- this is
                # the actual isolation mechanism now, not a cosmetic
                # nicety; a failure here must not silently fall through to
                # this connection sharing a tab with someone else. The
                # enclosing try/except below reports it as a normal failed
                # delegate_website_task result rather than crashing the run.
                tabs_tool = self._raw_tools_by_name.get("browser_tabs")
                if tabs_tool is not None:
                    await tabs_tool.coroutine(action="new")
            self.tools = [self._guard(t) for t in raw_tools]
            # request_human_help isn't a wrapped MCP tool -- it's our own
            # Python method, added directly to the model-facing toolset
            # (unlike _move_cursor_to/_start_reading_animation, which the
            # model never calls itself). Given to BOTH the top-level
            # provider and every sub-worker -- a login wall can turn up on
            # any tab, not just the first one.
            self.tools.append(StructuredTool.from_function(
                coroutine=self._request_human_help,
                name="request_human_help",
                description=(self._request_human_help.__doc__ or "").strip(),
            ))
            # Same reasoning as request_human_help just above -- a signup or
            # login form can turn up on any tab, top-level or sub-worker.
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
            # Given to BOTH the top-level provider and every sub-worker, same
            # reasoning as request_human_help just above -- any tab can want a
            # cheap read of some OTHER page before deciding whether it's worth
            # a real browser_navigate. This is a pure function (see
            # jina_reader.py) with no instance state of its own, so it's wired
            # in directly rather than as a bound method like the others above.
            self.tools.append(StructuredTool.from_function(
                coroutine=_jina_fetch_rendered_page_text,
                name="fetch_rendered_page_text",
                description=(
                    "Read a URL's rendered text WITHOUT using this browser session -- it runs "
                    "on a remote reader service, not this Browserbase session, so it costs NO "
                    "session time and doesn't touch this tab at all. Use it for reconnaissance "
                    "or comparison: checking what a candidate page currently says, or comparing "
                    "several candidate pages/sites, BEFORE deciding which one (if any) actually "
                    "needs browser_navigate and real interaction -- this is how a task that "
                    "combines reading and acting (e.g. 'find the cheapest of these and buy it') "
                    "should be done: compare with this tool first across every candidate, THEN "
                    "spend real browser/session time only on the one you're actually acting on. "
                    "Still read-only -- it can't click, type, scroll, or log in. If it comes "
                    "back empty or unhelpful for a page you genuinely need to read, fall back "
                    "to browser_navigate + browser_snapshot on it instead."
                ),
            ))
            # A second, independent provider for the exact same job as
            # fetch_rendered_page_text just above (Parallel Search MCP
            # instead of Jina Reader) -- same reasoning as request_human_help
            # for being given to both top-level and sub-workers: if Jina is
            # ever down or rate-limited, this is a real fallback that's
            # still free/no-session, not an automatic escalation straight to
            # spending more of this session's own browser time.
            self.tools.append(StructuredTool.from_function(
                coroutine=_parallel_web_fetch,
                name="parallel_web_fetch",
                description=(
                    "A second, independent provider for the SAME job as fetch_rendered_page_text "
                    "(a JS-rendered page read costing NO Browserbase session time) -- try this if "
                    "fetch_rendered_page_text comes back empty or errors out on a page you "
                    "genuinely need to read for comparison/reconnaissance. Also handles PDFs. "
                    "Still read-only. If this ALSO fails, fall back to browser_navigate + "
                    "browser_snapshot instead."
                ),
            ))
            # delegate_website_task is deliberately owning-provider-only --
            # _owns_server is the actual enforcement (not just leaving it
            # off a sub-worker's tool list), so there is no path to
            # recursive delegation even if this method were ever called
            # directly.
            if self._owns_server:
                self.tools.append(StructuredTool.from_function(
                    coroutine=self._delegate_website_task,
                    name="delegate_website_task",
                    description=(self._delegate_website_task.__doc__ or "").strip(),
                ))
            console.system(
                f"Deepsearch: launched with {len(self.tools)} tools "
                f"({'top-level' if self._owns_server else 'sub-worker ' + self._tab_id})."
            )
        except Exception as e:  # noqa: BLE001
            console.tool_error(LABEL, "playwright_mcp_connect", str(e))
            if self._owns_server:
                if self._mcp_proc is not None and self._mcp_proc.returncode is None:
                    self._mcp_proc.terminate()
                if self._bb_session_id is not None:
                    try:
                        await browserbase.release_session(self._bb_session_id)
                    except Exception as release_err:  # noqa: BLE001
                        console.tool_error(LABEL, "browserbase_release_after_failure", str(release_err))
            raise
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        # Belt-and-suspenders: guarded() already stops any in-flight reading
        # animation before every real action, so normally nothing is left
        # running by the time a task finishes. But if the run ended via an
        # exception/step-limit right after a browser_snapshot (before any
        # further guarded() call could fire the stop signal), a background
        # task could still be sitting there awaiting a browser_evaluate call
        # into a session we're about to tear down -- cancel it here so that
        # never turns into an "Attempted write to closed pipe"-style warning
        # on shutdown.
        if self._reading_task is not None and not self._reading_task.done():
            self._reading_task.cancel()
        if self._cursor_move_task is not None and not self._cursor_move_task.done():
            self._cursor_move_task.cancel()
        if self._session_cm is not None:
            await self._session_cm.__aexit__(exc_type, exc, tb)
        if not self._owns_server:
            # A sub-worker only closes its OWN client connection -- the
            # shared server process and the Browserbase session both belong
            # to the top-level provider, which tears those down (below)
            # once the whole deepsearch run ends, not when one delegated
            # sub-worker's single-site task finishes.
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
        if self._bb_session_id is not None:
            try:
                await browserbase.release_session(self._bb_session_id)
            except BrowserbaseError as e:
                # Best-effort: the session will idle out on its own either way.
                console.tool_error(LABEL, "browserbase_release", str(e))
        console.system("Deepsearch: browser closed.")

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
        if self._model is None or self._server_url is None:
            return "ERROR: delegate_website_task isn't available in this context."
        if not _domain_allowed(url):
            return f"BLOCKED: '{url}' is not in the allowed domain list."

        assert self._subagent_semaphore is not None  # set alongside server_url in __aenter__
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
                        except Exception as e:  # noqa: BLE001
                            console.tool_error(LABEL, "delegate_website_task", str(e))
                            summary = f"(errored: {e})"
                    finally:
                        if self._user_id is not None:
                            live_activity.clear_tab(self._user_id, worker._tab_id)
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

    def _guard(self, original: BaseTool) -> BaseTool:
        name = original.name
        state = self._state
        approval_gate = self._approval_gate

        async def guarded(*args: Any, **kwargs: Any) -> Any:
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

            if name in CURSOR_ANIMATED_TOOLS:
                self._fire_cursor_move(name, kwargs.get("element"), kwargs.get("target"))

            try:
                result = await original.coroutine(*args, **kwargs)
                state["consecutive_errors"] = 0
                console.tool_result(LABEL, name, result)
                self._live_add_step(action_desc)
                if name == "browser_snapshot":
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
                if state["consecutive_errors"] >= 2 and _looks_like_auth_wall(state["last_snapshot_text"]):
                    msg += (
                        " This looks like it might be a login/verification page you can't get "
                        "past on your own -- if so, call request_human_help with a short reason "
                        "instead of retrying again."
                    )
                return msg

        return StructuredTool.from_function(
            name=original.name,
            description=original.description,
            args_schema=original.args_schema,
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
    "fetch_rendered_page_text (or parallel_web_fetch if that fails) could answer it instead "
    "(comparing this site against others, confirming a fact, checking current content). Both "
    "cost no session time at all. Only use browser_navigate once you know THIS is the page you "
    "actually need to interact with.\n"
    "- After navigating, always call browser_snapshot to read actual page content. Prefer "
    "browser_find or browser_snapshot's `depth` argument over a full snapshot when you just "
    "need to confirm something worked.\n"
    "- browser_click/browser_type/browser_hover/browser_select_option/browser_drag accept "
    "EITHER an element ref from the most recent snapshot ('e12') OR a stable selector directly "
    "(e.g. 'role=button[name=\"Add to cart\"]', 'text=Submit', a CSS selector).\n"
    "- Base every factual claim strictly on text that literally appears in snapshots.\n"
    "- If a tool result starts with 'BLOCKED:' or 'ERROR', don't retry the same action -- take "
    "a fresh snapshot or try a different approach.\n"
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
    "regardless of how much or little you actually do with it. Before calling browser_navigate "
    "on any candidate page you're not already sure you need to interact with, ask whether "
    "fetch_rendered_page_text could answer it instead (parallel_web_fetch if that one fails) -- "
    "both run on a separate reader service and cost NOTHING against this session. For a task "
    "that combines reading/comparing with acting (e.g. 'check these 3 sites and buy from "
    "whichever is cheapest'), the right shape is: read every candidate FIRST with one of those "
    "tools, decide which one wins, THEN spend real browser_navigate/session time only on that "
    "one -- never navigate to a page purely to compare it against others when a fast read would "
    "tell you the same thing.\n"
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
    "- If a tool result starts with 'BLOCKED:' or 'ERROR', don't retry the same action -- take "
    "a fresh snapshot or try a different approach.\n"
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
    "- Don't know the right URL yet (a specific results page, not just a homepage)? Do one "
    "quick search yourself first (google.com or perplexity.ai, in your own tab) for a direct "
    "deep-link or reference fact, THEN delegate with that as your starting point -- a real head "
    "start, not a required step. Skip it once you already have a working URL, and never treat "
    "a search result as today's live price/availability -- delegate_website_task is still the "
    "source of truth for that.\n"
    "- When you're done, reply with a clear, complete, honest summary of what you found or did "
    "-- this goes straight back to the user.\n"
)


def build_deepsearch_subagent(
    user: UserContext, model: BaseChatModel, approval_gate: ApprovalGate | None = None
) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec. See module docstring for
    the open/close-per-task and session-resumption design."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
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

        incoming = state["messages"]
        last_text = _message_text(incoming[-1]) if incoming else ""
        match = _SESSION_REF_RE.search(last_text)

        session_row = None
        if match:
            session_row = await db.get_deepsearch_session(user.user_id, int(match.group(1)))

        if session_row:
            session_id = session_row["id"]
            task_title = session_row["title"]
            try:
                prior_messages = messages_from_dict(json.loads(session_row["messages"]))
            except Exception:
                prior_messages = []
            remainder = _SESSION_REF_RE.sub("", last_text).strip(" .:-\n")
            follow_up = HumanMessage(
                content=f"Continuing this task. Additional instruction: {remainder}"
                if remainder else "Continue where you left off."
            )
            messages = [*prior_messages, follow_up]
            steps_so_far = session_row["steps_used"]
            console.system(f"Deepsearch: resuming session #{session_id} ({len(prior_messages)} prior messages).")
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

        # live_view_url only gets set (and the "a browser is open right now"
        # DB flag only gets flipped on) *after* BrowserToolProvider.__aenter__
        # returns successfully -- so if opening the Browserbase session/CDP
        # connection itself fails, there's nothing to clear here, and the
        # try/finally below correctly never marks this user "live" for a
        # browser that never actually opened.
        #
        # live_activity.start() is called HERE, before BrowserToolProvider
        # even opens -- not after, like it used to be. This is the actual
        # fix for the real "only 1 tile ever shows, no matter how many tabs
        # are really open" bug: __aenter__ (below) creates the Browserbase
        # session and immediately calls live_activity.set_session_id() to
        # record bb_session_id, which server.py's _build_live_tiles needs to
        # look up the real pages[] and build one tile per tab. start()
        # unconditionally REPLACES this user's whole live_activity entry
        # with a fresh dict (see its own docstring/module comment) -- when it
        # ran AFTER __aenter__ returned, it was silently wiping the
        # bb_session_id that set_session_id had just written moments
        # earlier, every single time. With bb_session_id always back to None
        # by the time anyone polled /live/<token>/status, _build_live_tiles
        # always took its no-bb_session_id fallback path -- exactly one
        # tile, built from the session-level live_view_url, regardless of
        # how many tabs Browserbase actually had open. Starting it first
        # means __aenter__'s set_session_id call (which uses
        # _state.setdefault, not a fresh dict) lands on top of this same
        # entry instead of being overwritten by it.
        live_activity.start(user.user_id, task_title)
        live_view_url = None
        async with BrowserToolProvider(
            approval_gate, user_id=user.user_id, deepsearch_session_id=session_id, model=model,
            messa_email=user.messa_email,
        ) as provider:
            live_view_url = provider.live_view_url
            if live_view_url:
                await db.set_live_browser_active(user.user_id, live_view_url, task_title)
            try:
                _summarization = _deepsearch_summarization_middleware(model)
                inner_agent = create_agent(
                    model=model, tools=provider.tools, system_prompt=DEEPSEARCH_SYSTEM_PROMPT,
                    checkpointer=checkpointer,
                    middleware=[_summarization] if _summarization is not None else [],
                )
                status = "completed"
                try:
                    result = await asyncio.wait_for(
                        inner_agent.ainvoke({"messages": messages}, config=run_config),
                        timeout=config.DEEPSEARCH_MAX_SESSION_SECONDS,
                    )
                    final_messages = result["messages"]
                except GraphRecursionError:
                    status = "active"
                    state_snapshot = await inner_agent.aget_state(run_config)
                    final_messages = state_snapshot.values.get("messages", messages)
                    console.system(
                        f"Deepsearch: hit its {config.DEEPSEARCH_MAX_STEPS}-step limit -- "
                        f"saving progress to session #{session_id} for later."
                    )
                except asyncio.TimeoutError:
                    # Belt-and-suspenders session cap (config.DEEPSEARCH_MAX_SESSION_SECONDS)
                    # -- independent of DEEPSEARCH_MAX_STEPS (a step COUNT,
                    # not a time bound) and independent of
                    # request_human_help's own bounded wait, this is what
                    # actually guarantees a Browserbase session can't stay
                    # open indefinitely even if something upstream of here
                    # hangs. `async with` below still runs its normal
                    # teardown on the way out of this except block, same as
                    # any other exception.
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
                # Signal "closing" *before* this `async with` block ends --
                # the browser is still open and the DB still says a session
                # is live (we haven't cleared it yet), but the live-view
                # page's next poll can now proactively swap to a "Compiling
                # your results..." screen instead of riding out whatever
                # Browserbase's own embedded debug page renders the instant
                # its CDP connection is torn down (a raw "Debugging
                # connection was closed" banner) -- which happens moments
                # from now, when `provider.__aexit__` below actually
                # releases the Browserbase session.
                if live_view_url:
                    live_activity.set_closing(user.user_id)

        # BrowserToolProvider.__aexit__ has now released the Browserbase
        # session -- only clear "a browser is live" state once that's
        # actually true, mirroring the set_live_browser_active call above so
        # it runs whether the agent finished cleanly, hit its step limit, or
        # errored. Clearing this here (rather than in the finally above)
        # keeps the "closing" signal up for the whole teardown window
        # instead of flipping straight to idle before the front-end gets a
        # chance to show the "closing" screen.
        if live_view_url:
            await db.clear_live_browser_active(user.user_id)
            live_activity.clear(user.user_id)

        console.system(f"Deepsearch: whole run took {time.monotonic() - run_start:.2f}s (status={status}).")
        summary = _last_ai_text(final_messages)

        if session_id:
            await db.update_deepsearch_session(
                session_id,
                messages_json=json.dumps(messages_to_dict(final_messages), default=str),
                status=status,
                summary=summary,
                steps_used=steps_so_far + len(final_messages),
                live_view_url=live_view_url,
            )
            if status == "active":
                header = (
                    f"[deepsearch session #{session_id} -- not finished, hit its step limit. "
                    f"Say \"continue session #{session_id}\" to keep going.]\n"
                )
            else:
                header = f"[deepsearch session #{session_id} -- completed]\n"
        else:
            header = "[deepsearch -- session tracking not enabled: run migrations/004_deepsearch_sessions.sql]\n"

        return {"messages": [AIMessage(content=header + summary)]}

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
