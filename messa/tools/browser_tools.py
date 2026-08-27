"""Browser subagent tools -- Playwright MCP, guarded.

This ports the safety layer from the original `agent2.py` browser agent
(the one the project is modeled on) essentially unchanged:

  * domain allow-list on navigation,
  * destructive actions (typing, clicking, uploads, raw JS eval, dialogs)
    require human confirmation via the shared ApprovalGate,
  * element refs must come from a *fresh* snapshot -- if the page navigated
    since the last snapshot, click/type/hover/drag/select are refused until
    a new snapshot is taken, since stale refs silently point at the wrong
    element.

Generic tracing/error-handling now comes from `tools/common.trace_tool`
instead of being reimplemented here; this module only adds the
browser-specific rules on top.
"""
from __future__ import annotations

from urllib.parse import urlparse
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools

from .. import config, console
from ..approval import ApprovalGate

LABEL = "browser_agent"

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


def _domain_allowed(url: str) -> bool:
    if not config.BROWSER_ALLOWED_DOMAINS:
        return True
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in config.BROWSER_ALLOWED_DOMAINS)


class BrowserToolProvider:
    """Owns the Playwright MCP session for the process lifetime.

    Usage:
        async with BrowserToolProvider(approval_gate) as provider:
            tools = provider.tools
            ...build subagents using `tools`...
    """

    def __init__(self, approval_gate: ApprovalGate | None = None):
        self._approval_gate = approval_gate
        self._client = MultiServerMCPClient({
            "playwright": {
                "command": "npx",
                "args": ["@playwright/mcp@latest"],
                "transport": "stdio",
            }
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
        console.system(f"Browser agent: loaded {len(self.tools)} Playwright tools.")
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._session_cm is not None:
            await self._session_cm.__aexit__(exc_type, exc, tb)

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


BROWSER_SYSTEM_PROMPT = (
    "You are the browser automation specialist. You perform web browsing tasks "
    "delegated to you by Messa, the orchestrator.\n"
    "- Break the goal into steps.\n"
    "- After navigating, always call browser_snapshot to read actual page content.\n"
    "- Only use element refs from the MOST RECENT snapshot.\n"
    "- Base every factual claim strictly on text that literally appears in snapshots.\n"
    "- If a tool result starts with 'BLOCKED:' or 'ERROR', do not retry the exact same "
    "action; take a fresh snapshot or try a different approach.\n"
    "- When you're done, reply with a clear, complete summary of what you found or did. "
    "Be honest -- this summary goes straight back to the user.\n"
)
