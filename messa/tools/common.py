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

from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool, StructuredTool, tool as tool_decorator

from .. import console, usage
from ..approval import ApprovalGate
from ..config import UserContext
from ..reliability import unverified_claim_reason


async def run_inner_agent_with_claim_check(
    inner_agent: Any,
    messages: list[Any],
    run_config: dict[str, Any],
    label: str = "subagent",
) -> list[Any]:
    """Runs `inner_agent.ainvoke({"messages": messages}, config=run_config)`
    once; if the NEW messages it produced end in a reply that reads like an
    unverified completion claim (messa/reliability.py's
    unverified_claim_reason -- "I've sent it"/"all set"/"booked it" with no
    successful tool call behind it this delegation), replays with one nudge
    and re-invokes exactly once. Same bounded-single-retry shape as
    cli.py's run_turn uses for the orchestrator's own turn, and
    executive_tools.py's own separate past-due nudge-retry already uses for
    a different check -- never a second retry, so this can't itself become
    a loop.

    Returns the FULL final message list either way (the same shape
    `result["messages"]` always has) -- callers do
    `last_ai_text(await run_inner_agent_with_claim_check(...))` exactly as
    they'd do with a plain `(await inner_agent.ainvoke(...))["messages"]`.

    Deliberately passes the FULL accumulated message list (original +
    everything the first run produced + the nudge) on the retry, not just
    the nudge alone -- correct regardless of whether `inner_agent` has a
    checkpointer bound (most of these subagents don't), unlike a
    checkpointer-dependent "just send the nudge" shape that would silently
    lose all context for one with no checkpointer."""
    result = await inner_agent.ainvoke({"messages": messages}, config=run_config)
    final_messages = result["messages"]
    new_messages = final_messages[len(messages):]

    any_tool_call = any(getattr(m, "tool_calls", None) for m in new_messages)
    last_tool_content: Any = None
    for m in new_messages:
        if getattr(m, "type", None) == "tool":
            last_tool_content = getattr(m, "content", None)
    final_text = ""
    for m in reversed(new_messages):
        content = getattr(m, "content", "")
        if getattr(m, "type", None) == "ai" and content:
            final_text = content if isinstance(content, str) else str(content)
            break

    reason = unverified_claim_reason(final_text, any_tool_call, last_tool_content)
    if not reason:
        return final_messages

    console.system(f"{label}: {reason} -- retrying once with a nudge.")
    nudge = HumanMessage(
        content=(
            "(auto-check, not from the user: your previous reply claims something is "
            "done/sent/scheduled, but the tool trace doesn't back that up -- either no "
            "tool call actually did it, or the most recent one failed. Check the real "
            "result: if it actually failed, say so plainly instead of claiming success; "
            "if it should still happen, actually call the tool now.)"
        )
    )
    result2 = await inner_agent.ainvoke({"messages": final_messages + [nudge]}, config=run_config)
    return result2["messages"]


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
    feature: str | None = None,
    user: "UserContext | None" = None,
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
    as before -- this parameter changes nothing for them.

    feature/user: opt in to a usage-limit check (see ../usage.py) for a
    tool whose entire call is one costed unit -- e.g. email_tools.py's
    send_email tagged feature="outbound_emails". Both must be given
    together (a `feature` with no `user` can't be checked against a plan
    and is silently ignored) since most tools aren't metered at all. This
    runs BEFORE the destructive-approval gate below, not after: there's no
    point prompting the user to confirm an action that's already blocked
    by their plan. execute_integration_tool's per-slug email match is a
    separate, narrower check inside that tool itself (its single generic
    dispatcher covers far more than one feature), not this parameter --
    see tools/integration_tools.py."""

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

        if feature and user is not None:
            limit_result = await usage.check_and_consume(user, feature)
            if not limit_result.allowed:
                console.tool_result(label, name, limit_result.upgrade_message)
                return limit_result.upgrade_message

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
