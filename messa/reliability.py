"""Shared reliability primitives: empty-promise/unverified-claim detection
and the canonical tool-failure-string classifier.

Used by cli.py's run_turn (the orchestrator's own turn) AND every
subagent's own _run closure (deepsearch, executive_assistant, email_agent,
and -- after the CompiledSubAgent conversion in tools/integration_tools.py,
tools/personal_inbox_tools.py, tools/document_tools.py, tools/routines_tools.py,
tools/admin_tools.py -- all five of those too). Collected here once rather
than duplicated across six-plus files: exactly the kind of drift this
codebase's existing single-source-of-truth conventions (trace_tool's error
formatting, scratchpad_tools._scrub-style screening) already avoid
elsewhere, and getting the classifier/regex out of sync between call sites
would be worse than any one of them being imperfect.

Two distinct failure modes this addresses, both under the umbrella the
product owner calls "empty-promise loops":
  1. Messa/a subagent acknowledges intent ("Checking flights now...") but
     never actually calls the tool that would do it. cli.py's own,
     narrower `_STALL_PATTERN` + single bounded retry already catches
     exactly this one shape, at the orchestrator level only.
  2. Messa/a subagent states an action IS DONE ("I've sent the email",
     "booked it", "all set") when no tool call this turn/delegation
     actually confirmed it -- either no relevant tool ran at all, or the
     one that did came back looking like a failure. `unverified_claim_reason`
     below generalizes #1's exact bounded-single-retry shape (still exactly
     one retry, never a loop) to also catch #2, and applies it everywhere,
     not just at the top level.
"""
from __future__ import annotations

import re
from typing import Any

# Completion-claim phrasing -- same spirit and rigor as cli.py's own
# _STALL_PATTERN: broad enough to catch the real thing, narrow enough that
# an ordinary non-claim reply essentially never matches it by accident. A
# false positive here just costs one extra model call (the existing,
# accepted tradeoff `_STALL_PATTERN` itself already makes) -- a false
# negative (a real unverified claim slipping through) is the worse outcome
# this exists to minimize.
_COMPLETION_CLAIM_PATTERN = re.compile(
    r"\b(i'?ve (?:sent|scheduled|booked|created|added|deleted|removed|cancell?ed|"
    r"confirmed|finished|completed|updated|saved|set up|submitted|drafted and sent)\b|"
    r"\b(?:sent|scheduled|booked|created|added|cancell?ed|confirmed|submitted) it\b|"
    r"\ball set\b|\bit'?s (?:all )?done\b|\bthat'?s (?:all )?done\b|\ball done\b|"
    r"\b(?:sent|emailed) (?:it|that|this) (?:over|off)\b|"
    r"\bjust (?:sent|scheduled|booked|created|confirmed)\b)",
    re.IGNORECASE,
)

# Canonical error-string prefixes/shapes this codebase already uses,
# consistently, for a tool that failed WITHOUT raising an exception (see
# tools/common.py's trace_tool -- "ERROR running '{name}': {e}" --
# tools/integration_tools.py's execute_integration_tool -- "'{slug}'
# failed: {e}" -- and tools/integration_circuit_breaker.py's own
# short-circuit message -- "BLOCKED: ..."). Collected here once rather
# than re-guessed at each call site that wants to know "did the last tool
# result actually work."
_FAILURE_PREFIXES = ("ERROR running", "BLOCKED:", "Could not", "Can't", "Couldn't")
_FAILED_CALL_RE = re.compile(r"^'[^']+' failed:")
_NOT_SET_UP_RE = re.compile(r"isn'?t (?:set up|connected|enabled)\b", re.IGNORECASE)


def looks_like_tool_failure(content: Any) -> bool:
    """True if a tool RESULT string reads like a failure/refusal rather
    than a genuine success -- the same string-sniffing this codebase
    already does ad hoc in a few places, unified here so every caller
    agrees on what "the last tool call didn't actually work" means. Never
    raises: a non-string content (e.g. a Command, or structured content)
    is never treated as a failure by this heuristic -- false negatives
    here just mean an occasional unverified claim slips through
    undetected, the same accepted tradeoff `_STALL_PATTERN` already makes;
    a false positive would instead nudge/replay a perfectly good reply,
    the worse failure mode to bias against."""
    if not isinstance(content, str) or not content:
        return False
    if content.startswith(_FAILURE_PREFIXES):
        return True
    if _FAILED_CALL_RE.match(content):
        return True
    if _NOT_SET_UP_RE.search(content):
        return True
    return False


def unverified_claim_reason(
    final_text: str,
    any_tool_call: bool,
    last_tool_content: Any = None,
) -> str | None:
    """None if `final_text` needs no correction; otherwise a short,
    human-readable reason -- callers branch on `is not None` (the trigger
    to replay once with a nudge, mirroring cli.py's existing
    `_STALL_PATTERN` retry exactly), the string itself is only for
    console.system logging, same as that existing trip message.

    Fires when `final_text` reads like a completion claim
    (_COMPLETION_CLAIM_PATTERN) AND EITHER no tool call happened at all
    this turn/delegation, OR the most recent tool result
    `looks_like_tool_failure`. A real, tool-confirmed success is never
    flagged no matter how it's phrased -- this is about catching an
    UNSUPPORTED claim, never about policing wording."""
    if not final_text or not _COMPLETION_CLAIM_PATTERN.search(final_text):
        return None
    if not any_tool_call:
        return "reply claims something is done but made zero tool calls this turn"
    if looks_like_tool_failure(last_tool_content):
        return "reply claims something is done but the most recent tool result looks like a failure"
    return None


# Appended alongside scratchpad_tools.scratchpad_prompt_block's own
# task_block, everywhere that block is appended (registry.py's
# orchestrator system prompt, and every subagent's own _run) -- see each
# call site. Two rules, same family ("don't say something untrue or
# unprofessional to the user"): never claim an unconfirmed action
# succeeded, and never relay raw backend/API error text -- the exact "my
# Sheets read keeps glitching" incident
# scratchpad_work/docs/autonomous_integrations_and_task_memory_spec.md
# (3.5.2) names, confirmed nowhere in this codebase's prompts before this.
RELIABILITY_GUARDRAIL_STR = (
    "\n\n--- Say only what actually happened ---\n"
    "Never say something is done, sent, scheduled, created, or fixed unless a tool "
    "call in THIS response actually confirmed it succeeded. If a tool failed, "
    "errored, or you're not sure, say that plainly instead of a hopeful guess -- "
    "a clear 'that didn't go through, here's why' beats a false 'all set'.\n"
    "Never relay raw tool/API error text, error codes, stack traces, or exception "
    "messages to the user -- translate a failure into one plain, professional "
    "sentence about what happened and what you're doing about it."
)
