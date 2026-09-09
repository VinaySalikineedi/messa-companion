"""Generalized "Rule of 3" self-healing ladder for subagent tool calls
(feature/agentic-upgrade plan; originally
docs/smart_autonomous_agent_architecture.md's "zero-delta" rule for the
integrations side, now generalized beyond integrations_agent).

History: this module used to hold only `IntegrationRetryLoopMiddleware`,
which blocked `execute_integration_tool` once the EXACT SAME `(slug,
arguments)` pair failed twice. That missed the original incident's actual
shape entirely: 15+ failed calls against the same Google Sheets slug,
almost every one with DIFFERENT (wrong) arguments as the model kept
guessing -- `spreadsheet_name` instead of `spreadsheet_id`, then the title
in the wrong field, then a wildcard, then a wrong parameter name for the
same lookup. None of those repeats were "identical," so the old counter
never tripped once.

`ToolFailureLadderMiddleware` below fixes that by keying on the tool
IDENTITY alone (e.g. a Composio slug, or just the tool's own name for a
tool with no sub-identity like `send_email`) -- regardless of what
arguments were used. Three failures against the SAME identity, in ANY
combination of arguments, is what trips it -- a real 3-tier ladder, not a
flat cap:
  1st failure: return the error plus a hint to check the schema/skills
    before trying again.
  2nd failure: a firmer instruction -- look something up (schema, a saved
    skill, or a web search) BEFORE a third guess, not just try yet another
    argument shape.
  3rd failure: hard-block further attempts at that identity for the rest
    of the delegation, and tell the model plainly to stop and report the
    real blocker to the user -- ties directly into messa/reliability.py's
    "never claim an unconfirmed success" guardrail: a subagent that just
    got blocked here has no honest way to claim the action worked.

Reusable across subagents: `watched_tool_name` picks which tool call this
instance intercepts (e.g. "execute_integration_tool", "send_email",
"generate_pdf"); `identity_fn` (default: the tool name itself) extracts the
finer-grained identity from that call's arguments when one tool serves many
different underlying actions (execute_integration_tool's `slug`) -- a tool
with no such sub-identity just uses its own name as the identity, which
still gives a bounded stop instead of unlimited retries.

Before ANY of this existed, integrations_agent had ZERO retry protection --
confirmed by reading tools/common.py's trace_tool (every tool's exception
handler just returns a "don't retry with the exact same arguments" STRING,
a suggestion the model can ignore, never an enforced check) and registry.py's
declarative subagent dicts (no recursion_limit/step budget of any kind
before ModelCallLimitMiddleware was added alongside this).
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware, ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from .. import config, console
from ..reliability import looks_like_tool_failure


class ToolFailureLadderMiddleware(AgentMiddleware):
    """One instance per delegation (constructed fresh inside each
    subagent's own `_run`/`build_orchestrator` call -- these all run fresh
    every turn, same as `_summarization` in deepsearch_tools.py), watching
    ONE tool name. Attach several instances (one per watched tool) to a
    single subagent if it has more than one "do the real thing" tool worth
    guarding."""

    def __init__(
        self,
        watched_tool_name: str,
        *,
        identity_fn: Callable[[dict[str, Any]], str] | None = None,
        max_attempts: int | None = None,
        lookup_hint: str = "search_skills(...) for a known fix",
    ) -> None:
        self._watched_tool_name = watched_tool_name
        # Default identity: the tool's own name -- correct for a tool that
        # always does the same one kind of thing (send_email, generate_pdf).
        # execute_integration_tool passes identity_fn=lambda args:
        # args.get("slug") since ONE tool name dispatches to 1,400+
        # different underlying actions that must each get their own tally.
        self._identity_fn = identity_fn or (lambda _args: watched_tool_name)
        self._max_attempts = (
            max_attempts if max_attempts is not None else config.TOOL_FAILURE_LADDER_MAX_ATTEMPTS
        )
        self._lookup_hint = lookup_hint
        # identity -> total failure count this delegation (NOT "consecutive
        # identical" -- every failure on this identity counts, regardless
        # of arguments; that's the actual fix, see module docstring).
        self._failure_counts: dict[str, int] = {}
        # identities that have failed at least once -- used only for the
        # save_skill nudge on a later success, same as before.
        self._ever_failed: set[str] = set()

    @property
    def name(self) -> str:
        """Unique name per watched tool to satisfy langchain's uniqueness requirement."""
        return f"ToolFailureLadderMiddleware_{self._watched_tool_name}"

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != self._watched_tool_name:
            return await handler(request)

        args = request.tool_call.get("args") or {}
        identity = str(self._identity_fn(args) or self._watched_tool_name)
        prior_failures = self._failure_counts.get(identity, 0)

        if prior_failures >= self._max_attempts:
            console.system(
                f"{self._watched_tool_name}: failure-ladder breaker short-circuited a "
                f"{prior_failures + 1}th attempt at {identity!r} without running it."
            )
            return ToolMessage(
                content=(
                    f"BLOCKED: {identity!r} has already failed {prior_failures} time(s) in this "
                    "task -- repeating it (even with different arguments) will not help. Stop "
                    "trying this tool and tell the user plainly what's blocking this instead of "
                    "guessing again or claiming it worked."
                ),
                tool_call_id=request.tool_call["id"],
            )

        result = await handler(request)
        content = getattr(result, "content", "")
        failed = looks_like_tool_failure(content)

        if failed:
            new_count = prior_failures + 1
            self._failure_counts[identity] = new_count
            self._ever_failed.add(identity)
            if isinstance(result, ToolMessage):
                if new_count >= self._max_attempts:
                    tier_note = (
                        f"\n\n[This is failure #{new_count} on {identity!r} -- BLOCKED for the "
                        "rest of this task. Stop trying this. Tell the user plainly what's "
                        "blocking this rather than guessing again or claiming it worked.]"
                    )
                elif new_count >= 2:
                    tier_note = (
                        f"\n\n[This is failure #{new_count} on {identity!r}. Don't just guess "
                        f"another argument shape -- {self._lookup_hint} BEFORE trying "
                        f"{identity!r} again. One more failure blocks this tool for the rest of "
                        "this task.]"
                    )
                else:
                    tier_note = (
                        f"\n\n[This failed once on {identity!r} -- before retrying, "
                        f"{self._lookup_hint}.]"
                    )
                result = result.model_copy(update={"content": content + tier_note})
            return result

        # Succeeded. Clear this identity's tally (a later failure starts a
        # fresh count) and, if it failed earlier in this task, nudge the
        # model to capture what it learned -- steering behavior through
        # tool-result text, the same convention "Don't retry with the exact
        # same arguments" already uses everywhere in this codebase, rather
        # than a new mechanism. A nudge, not an automatic save_skill call:
        # turning a diff into a good problem_pattern/solution_recipe still
        # needs real judgment save_skill's own content-safety screening
        # (tools/scratchpad_tools.py) expects, not blind automation.
        self._failure_counts.pop(identity, None)
        if identity in self._ever_failed and isinstance(result, ToolMessage) and isinstance(content, str):
            nudge = (
                f"\n\n[This call to {identity!r} succeeded after an earlier failure in this task -- "
                "if you changed something non-obvious to fix it (a required parameter, a format "
                "quirk), call save_skill(...) now so this isn't rediscovered from scratch next "
                "time.]"
            )
            result = result.model_copy(update={"content": content + nudge})
        return result


def integration_slug_identity(args: dict[str, Any]) -> str:
    """identity_fn for execute_integration_tool -- the Composio slug being
    called IS the identity, not the generic dispatcher tool name (one tool
    name serves 1,400+ different underlying actions, each of which needs
    its own failure tally)."""
    return str(args.get("slug") or "execute_integration_tool")
