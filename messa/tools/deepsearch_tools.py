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
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict, messages_to_dict
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphRecursionError

from .. import config, console, db, live_activity
from ..approval import ApprovalGate
from ..channels import browserbase
from ..channels.browserbase import BrowserbaseError
from ..config import UserContext

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
    ):
        self._approval_gate = approval_gate
        self._user_id = user_id
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
        # Shared mutable state referenced by the closures below.
        self._state = {"consecutive_errors": 0, "snapshot_fresh": False}

    async def __aenter__(self) -> "BrowserToolProvider":
        # One Browserbase Context per user, created once and reused forever --
        # this is what makes logins survive between tasks (and, unlike the
        # old --user-data-dir profile directory, survives an HF Space
        # restart too, since it isn't on the container's disk at all).
        # This whole method used to have no top-level error handling (same as
        # the original local-Chromium version) -- fine when the only failure
        # mode was "npx/chromium missing," which the guarded tool-call path
        # below already logs clearly. Now that opening a browser means two
        # network round trips to Browserbase before anything else runs (auth,
        # quota, an expired/malformed key, HF's own egress to
        # api.browserbase.com being blocked -- all plausible, all silent
        # without this), a failure here needs its own clear, findable log
        # line instead of surfacing as an unlabeled exception three layers
        # up. Two separate try/excepts so the log line itself tells you
        # whether it was Browserbase or the local MCP/CDP connection step
        # that failed.
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
        except Exception as e:  # noqa: BLE001
            console.tool_error(LABEL, "browserbase_session_create", str(e))
            raise

        # Best-effort: not having a live-view link yet shouldn't block the run.
        try:
            self.live_view_url = await browserbase.get_live_view_url(self._bb_session_id)
        except BrowserbaseError as e:
            console.tool_error(LABEL, "browserbase_live_view", str(e))

        try:
            # env=dict(os.environ): MCP's stdio transport does NOT inherit the
            # parent process's environment by default (deliberately -- so an
            # arbitrary MCP server doesn't automatically see your secrets).
            # Without PATH/HOME passed through, the spawned npx subprocess
            # can't even find node_modules/npx's own cache -- still needed
            # here even though the browser itself is remote now.
            mcp_args = [
                "@playwright/mcp@latest", "--cdp-endpoint", connect_url,
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
            # Both cosmetic features (click/type cursor overlay, reading
            # scroll animation) live in the same asset file and ride the
            # same --init-script injection, but are independently toggled --
            # only skip injecting the script when BOTH are off.
            if config.DEEPSEARCH_CURSOR_OVERLAY or config.DEEPSEARCH_READING_ANIMATION:
                mcp_args += ["--init-script", str(_CURSOR_OVERLAY_SCRIPT_PATH)]
            self._client = MultiServerMCPClient({
                "playwright": {
                    "command": "npx",
                    "args": mcp_args,
                    "transport": "stdio",
                    "env": dict(os.environ),
                }
            })
            self._session_cm = self._client.session("playwright")
            self._session = await self._session_cm.__aenter__()
            raw_tools = await load_mcp_tools(self._session)
            self._raw_tools_by_name = {t.name: t for t in raw_tools}
            self.tools = [self._guard(t) for t in raw_tools]
            console.system(f"Deepsearch: launched with {len(self.tools)} Playwright tools.")
        except Exception as e:  # noqa: BLE001
            console.tool_error(LABEL, "browserbase_cdp_connect", str(e))
            # __aexit__ is NOT called by `async with` when __aenter__ itself
            # raises -- without this, a Browserbase session that was created
            # just above but never got a working CDP connection would leak
            # (left running until it idles out on Browserbase's side, rather
            # than released immediately). Left unfixed, repeated failed
            # attempts would each leak another session -- on the free plan's
            # low concurrent-session cap, that alone could make every
            # *subsequent* attempt fail at browserbase_session_create with a
            # quota/limit error, which would look identical to this failure
            # from the outside. Best-effort: we're already failing, a second
            # error here shouldn't mask the first.
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
        if self._session_cm is not None:
            await self._session_cm.__aexit__(exc_type, exc, tb)
        if self._bb_session_id is not None:
            try:
                await browserbase.release_session(self._bb_session_id)
            except BrowserbaseError as e:
                # Best-effort: the session will idle out on its own either way.
                console.tool_error(LABEL, "browserbase_release", str(e))
        console.system("Deepsearch: browser closed.")

    async def _move_cursor_to(self, element: str | None, target: str | None) -> None:
        """Best-effort, cosmetic-only: animate the injected SVG cursor (see
        cursor_overlay.js) to whatever element `target` resolves to, using
        the SAME target/ref the real action is about to use, so the arrow
        genuinely lands where the click will. Calls the RAW browser_evaluate
        tool directly -- never the guarded/model-facing one -- so this never
        prompts for human approval and is invisible to the model entirely.
        Any failure here (element not found, no cursor script loaded, a
        slow page) is swallowed: this must never block or fail the real
        action it's decorating."""
        if not config.DEEPSEARCH_CURSOR_OVERLAY or not target:
            return
        evaluate_tool = self._raw_tools_by_name.get("browser_evaluate")
        if evaluate_tool is None:
            return
        try:
            await evaluate_tool.coroutine(
                element=element or "target element", target=target, function=_CURSOR_MOVE_FN,
            )
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
            if self._user_id is not None:
                live_activity.set_description(self._user_id, action_desc)

            if name == "browser_navigate":
                url = kwargs.get("url") or (args[0] if args else None)
                if url and not _domain_allowed(url):
                    msg = f"BLOCKED: '{url}' is not in the allowed domain list."
                    console.tool_result(LABEL, name, msg)
                    if self._user_id is not None:
                        live_activity.add_step(self._user_id, f"Blocked: {url} isn't an allowed domain")
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
                    if self._user_id is not None:
                        live_activity.add_step(self._user_id, f"Blocked: you declined \"{action_desc}\"")
                    return msg
                state["snapshot_fresh"] = False

            if name in CURSOR_ANIMATED_TOOLS:
                await self._move_cursor_to(kwargs.get("element"), kwargs.get("target"))

            try:
                result = await original.coroutine(*args, **kwargs)
                state["consecutive_errors"] = 0
                console.tool_result(LABEL, name, result)
                if self._user_id is not None:
                    live_activity.add_step(self._user_id, action_desc)
                if name == "browser_snapshot":
                    # The agent is about to spend some think-time deciding
                    # its next move against this snapshot's content -- fill
                    # that dead time on the live view instead of leaving the
                    # page sitting frozen. Fire-and-forget; see
                    # _start_reading_animation's own docstring for why this
                    # is never awaited here.
                    self._start_reading_animation()
                return result
            except Exception as e:  # noqa: BLE001
                state["consecutive_errors"] += 1
                console.tool_error(LABEL, name, str(e))
                if self._user_id is not None:
                    live_activity.add_step(self._user_id, f"Error: {action_desc} failed ({e})")
                return (
                    f"ERROR running '{name}': {e}. Do not retry with the exact same "
                    f"arguments. Take a fresh browser_snapshot, then try a different approach."
                )

        return StructuredTool.from_function(
            name=original.name,
            description=original.description,
            args_schema=original.args_schema,
            coroutine=guarded,
        )


DEEPSEARCH_SYSTEM_PROMPT = (
    "You are deepsearch, the browser automation and research specialist. You perform web "
    "browsing tasks delegated to you by Messa, the orchestrator -- Messa only sends you tasks "
    "that genuinely need a real browser (clicking, forms, logins, carts, anything JS-rendered "
    "or interactive); a plain lookup she could answer with a quick search shouldn't reach you "
    "at all. You may be picking up a task you already made progress on in an earlier run -- if "
    "the message history already contains snapshots/navigation, you're continuing, not "
    "starting over; don't repeat completed steps.\n"
    "- Break the goal into steps.\n"
    "- After navigating, always call browser_snapshot to read actual page content.\n"
    "- Speed matters -- users notice how long this takes. Prefer the cheaper tool for what you "
    "actually need right now instead of defaulting to a full browser_snapshot every time:\n"
    "  - browser_find(text=... or regex=...) locates one specific element (and its ref) "
    "without capturing the whole page -- use it when you know what you're looking for.\n"
    "  - browser_snapshot's own `depth` argument returns a shallower tree when you just need "
    "to confirm something worked, not the full page layout.\n"
    "  - browser_click/browser_type/browser_hover/browser_select_option/browser_drag accept "
    "EITHER an element ref from a snapshot ('e12') OR a stable selector directly (e.g. "
    "'role=button[name=\"Add to cart\"]', 'text=Submit', a CSS selector) -- a stable selector "
    "doesn't require a fresh snapshot first, so reuse one directly for a repeat interaction "
    "(e.g. clicking the same kind of button again) instead of re-snapshotting.\n"
    "- Only use an element REF from the MOST RECENT snapshot -- a ref from an older snapshot "
    "may point at the wrong element if the page changed since. This restriction doesn't apply "
    "to selector-style targets, which Playwright resolves fresh every time.\n"
    "- Base every factual claim strictly on text that literally appears in snapshots.\n"
    "- If a tool result starts with 'BLOCKED:' or 'ERROR', do not retry the exact same "
    "action; take a fresh snapshot or try a different approach.\n"
    "- When you're done, reply with a clear, complete summary of what you found or did. "
    "Be honest -- this summary goes straight back to the user.\n"
)


def build_deepsearch_subagent(
    user: UserContext, model: BaseChatModel, approval_gate: ApprovalGate | None = None
) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec. See module docstring for
    the open/close-per-task and session-resumption design."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
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
        }

        # live_view_url only gets set (and the "a browser is open right now"
        # DB flag only gets flipped on) *after* BrowserToolProvider.__aenter__
        # returns successfully -- so if opening the Browserbase session/CDP
        # connection itself fails, there's nothing to clear here, and the
        # try/finally below correctly never marks this user "live" for a
        # browser that never actually opened.
        live_view_url = None
        async with BrowserToolProvider(approval_gate, user_id=user.user_id) as provider:
            live_view_url = provider.live_view_url
            if live_view_url:
                await db.set_live_browser_active(user.user_id, live_view_url, task_title)
                # Starts this task's chain-of-thought log (see live_activity.py)
                # -- guarded() below fills it in as tool calls actually happen;
                # cleared in the finally block regardless of how this run ends.
                live_activity.start(user.user_id, task_title)
            try:
                inner_agent = create_agent(
                    model=model, tools=provider.tools, system_prompt=DEEPSEARCH_SYSTEM_PROMPT,
                    checkpointer=checkpointer,
                )
                status = "completed"
                try:
                    result = await inner_agent.ainvoke({"messages": messages}, config=run_config)
                    final_messages = result["messages"]
                except GraphRecursionError:
                    status = "active"
                    state_snapshot = await inner_agent.aget_state(run_config)
                    final_messages = state_snapshot.values.get("messages", messages)
                    console.system(
                        f"Deepsearch: hit its {config.DEEPSEARCH_MAX_STEPS}-step limit -- "
                        f"saving progress to session #{session_id} for later."
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
            "exactly where it left off instead of starting over."
        ),
        "runnable": RunnableLambda(_run),
    }
