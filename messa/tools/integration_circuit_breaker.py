"""Circuit breaker for `integrations_agent` (the generic Composio dispatcher
in tools/integration_tools.py) -- the reliability-hardening pass's answer to
docs/smart_autonomous_agent_architecture.md's "zero-delta" rule for the
integrations side.

Deepsearch's own zero-delta breaker (tools/browser_circuit_breaker.py)
fingerprints page state because a browser action can silently "succeed"
with no visible change. Composio tool calls don't have that ambiguity --
`execute_integration_tool` either returns a clean result or a `"'{slug}'
failed: ..."` string (see that function's own except block), so "state" here
is simply: did this EXACT call (same slug, same arguments) already fail?
Repeating it again with nothing changed is exactly the same failure class
as the browser side's zero-delta loop, just with a much cheaper, more
reliable signal than a DOM/URL fingerprint.

Before this module, integrations_agent had ZERO retry protection anywhere
-- confirmed by reading tools/common.py's trace_tool (every tool's
exception handler just returns a "don't retry with the exact same
arguments" STRING, which is a suggestion the model can ignore, never an
enforced check) and registry.py's declarative `integrations_agent` dict
(no recursion_limit/step budget of any kind). This middleware, plus
ModelCallLimitMiddleware wired in alongside it in registry.py, are the
first real guardrails this subagent has ever had.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from .. import config, console

_EXECUTE_TOOL_NAME = "execute_integration_tool"


def _args_key(arguments: Any) -> str:
    """Stable string key for a call's arguments, order-independent (a model
    re-emitting the same logical call with keys in a different order must
    still count as identical). Falls back to `repr` for anything that
    can't be JSON-serialized (e.g. a stray non-JSON-safe value) rather than
    raising -- a key-computation hiccup must never be why a real tool call
    doesn't execute."""
    try:
        return json.dumps(arguments, sort_keys=True, default=str)
    except Exception:
        return repr(arguments)


class IntegrationRetryLoopMiddleware(AgentMiddleware):
    """One instance per orchestrator build (registry.py's `build_orchestrator`
    runs fresh every turn -- see its own call site in cli.py -- so this
    middleware's state, like `_summarization` in deepsearch_tools.py, has no
    reason to persist beyond one turn: a plain instance attribute, not a DB
    row or module-level global)."""

    def __init__(self, max_identical_attempts: int | None = None) -> None:
        self._max_identical_attempts = (
            max_identical_attempts
            if max_identical_attempts is not None
            else config.INTEGRATION_RETRY_LOOP_MAX_IDENTICAL_ATTEMPTS
        )
        # (slug, args_key) -> consecutive identical-failure count.
        self._failure_counts: dict[tuple[str, str], int] = {}
        # slugs that have failed at least once in this delegation, regardless
        # of exact arguments -- used only for the save_skill nudge below.
        self._ever_failed_slugs: set[str] = set()

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != _EXECUTE_TOOL_NAME:
            return await handler(request)

        args = request.tool_call.get("args") or {}
        slug = str(args.get("slug") or "")
        key = (slug, _args_key(args.get("arguments")))
        prior_failures = self._failure_counts.get(key, 0)

        if prior_failures >= self._max_identical_attempts:
            console.system(
                f"integrations_agent: retry-loop breaker short-circuited a "
                f"{prior_failures + 1}th identical call to {slug!r} without reaching Composio."
            )
            return ToolMessage(
                content=(
                    f"BLOCKED: '{slug}' has already failed {prior_failures} time(s) with these "
                    "exact arguments in this task. Repeating the identical call again will not "
                    f"help -- call describe_integration_tool({slug!r}) to check the real "
                    "parameter schema, call search_skills for a known fix, or change your "
                    "arguments before trying again."
                ),
                tool_call_id=request.tool_call["id"],
            )

        result = await handler(request)
        content = getattr(result, "content", "")
        failed = isinstance(content, str) and content.startswith(f"'{slug}' failed:")

        if failed:
            self._failure_counts[key] = prior_failures + 1
            self._ever_failed_slugs.add(slug)
            return result

        # Succeeded. Clear this exact key's failure count (a later call with
        # these same arguments starts its own fresh count) and, if this slug
        # failed earlier in the task under DIFFERENT arguments, nudge the
        # model to capture what it learned -- steering behavior through
        # tool-result text, the same convention "Don't retry with the exact
        # same arguments" already uses everywhere in this codebase, rather
        # than a new mechanism. A nudge, not an automatic save_skill call:
        # turning a diff into a good problem_pattern/solution_recipe still
        # needs real judgment save_skill's own content-safety screening
        # (tools/scratchpad_tools.py) expects, not blind automation.
        self._failure_counts.pop(key, None)
        if slug in self._ever_failed_slugs and isinstance(result, ToolMessage) and isinstance(content, str):
            nudge = (
                f"\n\n[This call to '{slug}' succeeded after an earlier failure in this task -- "
                "if you changed something non-obvious to fix it (a required parameter, a format "
                "quirk), call save_skill(...) now so this isn't rediscovered from scratch next "
                "time.]"
            )
            result = result.model_copy(update={"content": content + nudge})
        return result
