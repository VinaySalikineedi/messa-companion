"""Shared tool-wrapping helper used by every subagent's tool module.

Generalizes the `guard_tool` pattern from the original browser agent
(agent2.py) so every tool in the harness -- not just browser tools -- gets:

  * real-time console tracing (see messa/console.py for why this beats
    trying to stream nested subagent events),
  * consistent error handling (exceptions become a string the agent can
    react to instead of crashing the whole run),
  * optional human confirmation for actions marked destructive.

Works for both plain @tool-decorated functions and StructuredTool instances
(e.g. tools loaded from an MCP server).
"""
from __future__ import annotations

import inspect
from typing import Any, Callable, Sequence

from langchain_core.tools import BaseTool, StructuredTool, tool as tool_decorator

from .. import console
from ..approval import ApprovalGate


def last_ai_text(messages: list[Any]) -> str:
    """The most recent non-empty AIMessage's text content, walking backward
    -- what a small-loop `CompiledSubAgent` (executive_assistant,
    email_agent) hands back to the orchestrator as its own final reply,
    once its inner `create_agent` run finishes. Shared here (rather than
    duplicated per subagent module) since both need the exact same "last
    thing the model actually said" extraction. Falls back to a plain
    "Done." if the run somehow ended with no AI text at all (e.g. the
    model's very last message was a tool call whose result never got a
    follow-up reply before the loop ended) -- still a real string, so the
    orchestrator has something to relay rather than crashing on empty
    content."""
    for m in reversed(messages):
        content = getattr(m, "content", "")
        if getattr(m, "type", None) == "ai" and content:
            return content if isinstance(content, str) else str(content)
    return "Done."


def trace_tool(
    original: BaseTool,
    label: str,
    *,
    destructive: bool = False,
    approval_gate: ApprovalGate | None = None,
    destructive_check: Callable[..., bool] | None = None,
) -> BaseTool:
    """Wrap a tool with tracing, error handling, and optional confirmation.

    destructive_check: for the rare tool where "is this call destructive"
    can't be known until you see the ARGUMENTS (e.g.
    tools/integration_tools.py's execute_integration_tool -- one tool that
    can run anything from a read-only Composio action to an irreversible
    one, depending on which `slug` it's called with this time). When given,
    it's called with this invocation's own (*args, **kwargs) and its
    return value decides gating for THIS call, overriding the static
    `destructive` flag. Every other tool in this app has a fixed, known-at-
    registration-time destructiveness (send_email is always destructive,
    list_recent_emails never is) and just uses the plain `destructive` bool
    as before -- this parameter changes nothing for them."""

    name = original.name
    is_async_native = original.coroutine is not None

    async def _invoke_original(*args: Any, **kwargs: Any) -> Any:
        if is_async_native:
            return await original.coroutine(*args, **kwargs)
        result = original.func(*args, **kwargs)  # type: ignore[misc]
        if inspect.isawaitable(result):
            result = await result
        return result

    async def guarded(*args: Any, **kwargs: Any) -> Any:
        display_args = kwargs if kwargs else {"args": args}
        console.tool_call(label, name, display_args)

        is_destructive_this_call = destructive_check(*args, **kwargs) if destructive_check else destructive
        if is_destructive_this_call:
            gate = approval_gate or _NO_APPROVAL_GATE
            allowed = await gate.confirm(label, name, display_args)
            if not allowed:
                msg = f"BLOCKED: user declined to run '{name}'."
                console.tool_result(label, name, msg)
                return msg

        try:
            result = await _invoke_original(*args, **kwargs)
            console.tool_result(label, name, result)
            return result
        except Exception as e:  # noqa: BLE001 - tools must never crash the agent loop
            console.tool_error(label, name, str(e))
            return (
                f"ERROR running '{name}': {e}. Do not retry with the exact same "
                f"arguments -- try a different approach."
            )

    return StructuredTool.from_function(
        name=original.name,
        description=original.description,
        args_schema=original.args_schema,
        coroutine=guarded,
    )


class _NoApprovalGate:
    async def confirm(self, label: str, tool_name: str, args: dict[str, Any]) -> bool:
        console.system(f"No approval gate configured for destructive tool '{tool_name}' -- denying by default.")
        return False


_NO_APPROVAL_GATE = _NoApprovalGate()


def trace_all(
    tools: Sequence[BaseTool],
    label: str,
    *,
    destructive_names: set[str] | None = None,
    approval_gate: ApprovalGate | None = None,
) -> list[BaseTool]:
    destructive_names = destructive_names or set()
    return [
        trace_tool(t, label, destructive=t.name in destructive_names, approval_gate=approval_gate)
        for t in tools
    ]
