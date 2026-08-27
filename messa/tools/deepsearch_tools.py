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

Lifecycle: `build_deepsearch_subagent()` returns a deepagents
`CompiledSubAgent` whose runnable launches `@playwright/mcp` fresh on each
delegation and fully closes that subprocess (killing the browser) once the
subagent's task finishes -- so an idle Messa session isn't holding a
Chromium process open. Logins still survive between tasks because each
user gets a persistent on-disk profile directory (`--user-data-dir`, keyed
by user id) rather than an in-memory (`--isolated`) one.

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

from .. import config, console, db
from ..approval import ApprovalGate
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


def _domain_allowed(url: str) -> bool:
    if not config.DEEPSEARCH_ALLOWED_DOMAINS:
        return True
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in config.DEEPSEARCH_ALLOWED_DOMAINS)


def _profile_dir(user_id: int) -> str:
    path = Path(config.DEEPSEARCH_PROFILES_ROOT) / f"user-{user_id}"
    path.mkdir(parents=True, exist_ok=True)
    return str(path.resolve())


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
    """Owns one Playwright MCP subprocess for the duration of an `async with` block.

    Usage:
        async with BrowserToolProvider(approval_gate, user_id=1) as provider:
            tools = provider.tools
            ...use tools while the block is open...
        # subprocess (and its browser) is fully closed here.
    """

    def __init__(
        self,
        approval_gate: ApprovalGate | None = None,
        user_id: int | None = None,
        headless: bool | None = None,
    ):
        self._approval_gate = approval_gate
        # Deliberately no --browser flag: passing "chromium" explicitly
        # (tempting, given the Dockerfile only installs "chromium") actually
        # makes this *worse* on current @playwright/mcp versions -- it maps
        # to a separate "Chrome for Testing" build with its own install
        # command (`install-browser chrome-for-testing`), not the Chromium
        # `playwright install chromium` puts on disk. Confirmed by testing
        # both ways: omitting --browser correctly finds and launches the
        # Dockerfile's installed Chromium (including in --user-data-dir/
        # persistent-context mode, matching deepsearch's actual usage);
        # passing --browser chromium instead fails looking for an
        # executable that was never installed.
        args = ["@playwright/mcp@latest"]
        if user_id is not None:
            args += ["--user-data-dir", _profile_dir(user_id)]
        if headless if headless is not None else config.DEEPSEARCH_HEADLESS:
            args.append("--headless")
        # env=dict(os.environ): MCP's stdio transport does NOT inherit the
        # parent process's environment by default (deliberately -- so an
        # arbitrary MCP server doesn't automatically see your secrets).
        # Without this, the spawned npx subprocess has no PATH/HOME/
        # PLAYWRIGHT_BROWSERS_PATH, so it can't find the Chromium the
        # Dockerfile installed either -- this and the --browser flag above
        # were BOTH needed, not just one or the other.
        self._client = MultiServerMCPClient({
            "playwright": {"command": "npx", "args": args, "transport": "stdio", "env": dict(os.environ)}
        })
        self._session_cm = None
        self._session = None
        self.tools: list[BaseTool] = []
        # Shared mutable state referenced by the closures below.
        self._state = {"consecutive_errors": 0, "snapshot_fresh": False}

    async def __aenter__(self) -> "BrowserToolProvider":
        self._session_cm = self._client.session("playwright")
        self._session = await self._session_cm.__aenter__()
        raw_tools = await load_mcp_tools(self._session)
        self.tools = [self._guard(t) for t in raw_tools]
        console.system(f"Deepsearch: launched with {len(self.tools)} Playwright tools.")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(exc_type, exc, tb)
        console.system("Deepsearch: browser closed.")

    def _guard(self, original: BaseTool) -> BaseTool:
        name = original.name
        state = self._state
        approval_gate = self._approval_gate

        async def guarded(*args: Any, **kwargs: Any) -> Any:
            display_args = kwargs if kwargs else {"args": args}
            console.tool_call(LABEL, name, display_args)

            if name == "browser_navigate":
                url = kwargs.get("url") or (args[0] if args else None)
                if url and not _domain_allowed(url):
                    msg = f"BLOCKED: '{url}' is not in the allowed domain list."
                    console.tool_result(LABEL, name, msg)
                    return msg
                state["snapshot_fresh"] = False

            if name == "browser_snapshot":
                state["snapshot_fresh"] = True

            if name in SNAPSHOT_DEPENDENT_TOOLS and not state["snapshot_fresh"]:
                msg = (
                    "ERROR: You must call browser_snapshot before using element refs. "
                    "The page may have changed since your last snapshot. Take a fresh "
                    "snapshot now, then retry this action with a valid ref."
                )
                console.tool_result(LABEL, name, msg)
                return msg

            if name in DESTRUCTIVE_TOOLS:
                gate = approval_gate
                allowed = await gate.confirm(LABEL, name, display_args) if gate else False
                if not allowed:
                    msg = f"BLOCKED: user declined to run '{name}'."
                    console.tool_result(LABEL, name, msg)
                    return msg
                state["snapshot_fresh"] = False

            try:
                result = await original.coroutine(*args, **kwargs)
                state["consecutive_errors"] = 0
                console.tool_result(LABEL, name, result)
                return result
            except Exception as e:  # noqa: BLE001
                state["consecutive_errors"] += 1
                console.tool_error(LABEL, name, str(e))
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
    "browsing tasks delegated to you by Messa, the orchestrator. You may be picking up a "
    "task you already made progress on in an earlier run -- if the message history already "
    "contains snapshots/navigation, you're continuing, not starting over; don't repeat "
    "completed steps.\n"
    "- Break the goal into steps.\n"
    "- After navigating, always call browser_snapshot to read actual page content.\n"
    "- Only use element refs from the MOST RECENT snapshot.\n"
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
            title = (last_text.strip() or "Deepsearch task")[:255]
            created = await db.create_deepsearch_session(user.user_id, title)
            session_id = created["id"] if created else None
            steps_so_far = 0
            if session_id:
                console.system(f"Deepsearch: started session #{session_id}.")

        checkpointer = MemorySaver()
        run_config = {
            "configurable": {"thread_id": f"deepsearch-{session_id or 'untracked'}"},
            "recursion_limit": config.DEEPSEARCH_MAX_STEPS,
        }

        async with BrowserToolProvider(approval_gate, user_id=user.user_id) as provider:
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

        summary = _last_ai_text(final_messages)

        if session_id:
            await db.update_deepsearch_session(
                session_id,
                messages_json=json.dumps(messages_to_dict(final_messages), default=str),
                status=status,
                summary=summary,
                steps_used=steps_so_far + len(final_messages),
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
