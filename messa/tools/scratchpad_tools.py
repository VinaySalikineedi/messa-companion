"""Active Task Scratchpad + persistent Skills Playbook -- shared across
EVERY subagent and the orchestrator itself
(docs/autonomous_integrations_and_task_memory_spec.md).

Built from a real production incident: Messa forgot her own pre-written
pitch draft mid-task once older messages rolled off (message-history
truncation), and integrations_agent made 15+ failed tool calls
re-discovering that Google Sheets requires a spreadsheet_id it already had
two turns earlier. This module is the fix for both, generalized to every
subagent rather than bolted onto integrations_agent alone -- registry.py's
build_orchestrator attaches build_scratchpad_tools(...)'s output to every
declarative subagent's own tool list plus the orchestrator's, and each
CompiledSubAgent (deepsearch, email_agent, executive_assistant) attaches it
inside its own inner _run the same way -- rather than any single subagent
module wiring this up for itself.

Four tools:
  - update_task_scratchpad(fields): merge structured facts (spreadsheet_id,
    pitch_draft, recipients, ...) into the CURRENT active task so they
    survive message-history truncation/compaction/summarization. Starts a
    task automatically if none is open yet. Size-capped (config.
    SCRATCHPAD_FIELD_MAX_CHARS per field, SCRATCHPAD_TOTAL_MAX_CHARS total)
    so one runaway agent can't balloon a single row without limit.
  - complete_task(summary): mark the current active task 'completed' once
    it's actually done. This is the PRIMARY way a task leaves 'in_progress'
    -- without it, a task only ever clears via the time-based auto-abandon
    safety net in db.purge_stale_active_tasks, which exists for crashes/
    dropped threads, not as the normal completion path.
  - save_skill(domain, problem_pattern, solution_recipe): persist a short,
    reusable lesson to the GLOBAL, cross-user skills playbook -- a tool's
    real parameter shape, a site's navigation quirk, a deprecated action to
    avoid -- so every future task (this user's and every other user's)
    benefits immediately instead of re-discovering it from scratch.
  - search_skills(domain): look up this agent's own learned lessons for a
    toolkit/site (e.g. "googlesheets", "amazon.com") before diving in.

`agent_type` is NEVER a model-supplied argument on any of these three --
it's closed over from build_scratchpad_tools(agent_type=...)'s own
parameter, fixed once per subagent at construction time in registry.py (or
each CompiledSubAgent's own _run). A model asked to self-report which
agent it is would occasionally get it wrong, or could be prompted to
misreport it (e.g. to write into a differently-trusted bucket) -- closing
over the real value server-side removes that whole class of mistake/misuse
entirely, rather than trying to validate a free-text argument after the
fact.
"""
from __future__ import annotations

import re
from typing import Any

from langchain_core.tools import BaseTool, tool

from .. import config, console, db
from ..approval import ApprovalGate
from .common import trace_tool

# ---------------------------------------------------------------------------
# Content-safety screening for save_skill.
#
# Why this matters more than a normal input validator: skills are GLOBAL
# and get injected as trusted system-prompt text into every future user's
# session. That makes save_skill a structurally different risk than a
# one-turn prompt injection -- a single bad row is persistent and its
# blast radius is every user, not just this conversation. Two realistic
# attack shapes this screens for: (1) an agent naively "learns" hidden
# adversarial text encountered on a malicious webpage or in an email body
# during its own research, and tries to save it verbatim as a "lesson";
# (2) a user deliberately tries to get destructive or data-exfiltrating
# instructions saved as a "skill" that other users' agents will later
# inherit and trust.
#
# Defense in depth, not one silver bullet:
#   1. Hard length caps (config.SKILL_*_MAX_CHARS) -- a real lesson is a
#      short slug + 1-2 factual sentences, never a paragraph of scraped
#      page/email text. This alone shrinks how much an attacker could ever
#      smuggle through this path.
#   2. Banned-phrase matching against classic prompt-injection/override
#      language and destructive/exfiltration verbs.
#   3. No URLs or email addresses in the recipe -- a legitimate procedural
#      lesson never needs to embed a link or address; both are exactly
#      what a real exfiltration attempt would need to include.
#
# Deliberately conservative: a false-positive rejection just means one
# lesson doesn't get saved (no real harm, the agent still completes its
# actual task); a false negative is the one thing this function exists to
# minimize, so it errs toward rejecting anything merely resembling the
# banned shapes rather than trying to be clever about intent.
# ---------------------------------------------------------------------------
_BANNED_PHRASE_PATTERNS = (
    # Up to a few words of qualifiers between the verb and "instructions"
    # (e.g. "ignore all previous instructions") rather than requiring
    # exactly one, per the "err toward rejecting" design in this module's
    # own docstring.
    r"ignore\b[\s\w]{0,25}\binstructions\b",
    r"disregard\b[\s\w]{0,25}\binstructions\b",
    r"system prompt",
    r"you are now",
    r"act as",
    r"always (do|send|share|delete|disable|bypass|skip|approve|allow|trust|accept)\b",
    r"never (ask|confirm|verify)\b",
    r"without\s+(confirmation|confirming|asking|verifying|verification)",
    r"api[_ -]?key",
    r"\bpassword\b",
    r"\bsecret\b",
    r"\bcredential",
    r"send (it|this|that|data|info|details) to",
    r"forward (it|this|that|data|info) to",
    r"delete all",
    r"drop table",
    r"exfiltrat",
)
_BANNED_PHRASE_RE = re.compile("|".join(_BANNED_PHRASE_PATTERNS), re.IGNORECASE)
_URL_OR_EMAIL_RE = re.compile(r"https?://|[\w.+-]+@[\w-]+\.[a-z]{2,}", re.IGNORECASE)


def _screen_skill_text(problem_pattern: str, solution_recipe: str) -> str | None:
    """None if the text passes screening, else a short human-readable
    rejection reason (returned directly to the calling agent as the tool
    result, so a rejected save is a normal, visible outcome it can react
    to -- never a silent drop, never a raised exception)."""
    if not problem_pattern.strip() or not solution_recipe.strip():
        return "problem_pattern and solution_recipe can't be empty."
    if len(problem_pattern) > config.SKILL_PROBLEM_PATTERN_MAX_CHARS:
        return f"problem_pattern too long (max {config.SKILL_PROBLEM_PATTERN_MAX_CHARS} chars) -- keep it a short slug."
    if len(solution_recipe) > config.SKILL_SOLUTION_RECIPE_MAX_CHARS:
        return f"solution_recipe too long (max {config.SKILL_SOLUTION_RECIPE_MAX_CHARS} chars) -- keep it to 1-2 sentences."
    combined = f"{problem_pattern}\n{solution_recipe}"
    if _BANNED_PHRASE_RE.search(combined):
        return "rejected: this reads like an instruction/override rather than a factual tool or site lesson."
    if _URL_OR_EMAIL_RE.search(combined):
        return "rejected: a skill should never contain a URL or email address."
    return None


def _validate_scratchpad_fields(fields: dict[str, Any]) -> str | None:
    """None if `fields` passes the size guardrails, else a short
    human-readable rejection reason returned directly to the calling agent
    (never a silent drop, never a raised exception -- same contract as
    _screen_skill_text above). Caps a single field's *value* (stringified)
    at config.SCRATCHPAD_FIELD_MAX_CHARS, and the whole payload
    (stringified) at config.SCRATCHPAD_TOTAL_MAX_CHARS, so one field with a
    huge scraped blob or a `fields` dict with many merged calls over time
    can't grow a single active_tasks row -- and therefore every future
    prompt that reloads it -- without limit."""
    total_len = 0
    for key, value in fields.items():
        value_str = value if isinstance(value, str) else repr(value)
        total_len += len(str(key)) + len(value_str)
        if len(value_str) > config.SCRATCHPAD_FIELD_MAX_CHARS:
            return (
                f"field {key!r} is too long ({len(value_str)} chars, max "
                f"{config.SCRATCHPAD_FIELD_MAX_CHARS}) -- store a summary or "
                "id, not the full content."
            )
    if total_len > config.SCRATCHPAD_TOTAL_MAX_CHARS:
        return (
            f"fields payload too large ({total_len} chars, max "
            f"{config.SCRATCHPAD_TOTAL_MAX_CHARS}) -- trim old fields you no "
            "longer need before adding new ones."
        )
    return None


def _format_task_block(task: dict[str, Any] | None) -> str:
    """Shared prompt text appended to the orchestrator's AND every
    subagent's system prompt (see this module's own docstring for the full
    list of call sites). Describes the two tools generically and, when a
    real task is open, inlines its artifacts directly -- the actual fix
    for the "Messa forgot her own pitch draft" bug, since this text is
    part of the system prompt itself and so is immune to the conversation's
    OWN message history being truncated or summarized."""
    lines = [
        "\n\n--- Active task memory & shared skills playbook ---",
        "update_task_scratchpad(fields): call this the MOMENT you learn or produce "
        "something a multi-step task will need again (an id, a draft, a recipient "
        "list) -- not at the end. Never ask the user to re-paste something you "
        "already produced earlier in this same task; check the artifacts below "
        "first. Keep field values short (ids/summaries, not full scraped "
        "content) -- oversized fields are rejected.",
        "complete_task(summary): call this once the current task is actually "
        "done, so it stops being carried into unrelated future conversations. "
        "Don't leave a finished task open.",
        "search_skills(domain) / save_skill(domain, problem_pattern, solution_recipe): "
        "call search_skills for a toolkit or website BEFORE working with it, "
        "especially right after a confusing error -- someone (you or another "
        "agent) may have already solved this exact problem. Call save_skill once "
        "you've actually resolved something non-obvious, so it's never "
        "rediscovered from scratch again. A skill is a short factual lesson, "
        "never an instruction -- it will be rejected automatically if it reads "
        "like one, or if it contains a URL/email/credential.",
    ]
    if task and task.get("artifacts"):
        lines.append(f"Current active task ({task.get('task_type')}) artifacts so far: {task['artifacts']}")
    return "\n".join(lines)


async def scratchpad_prompt_block(user: config.UserContext) -> str:
    """Async convenience wrapper for CompiledSubAgent call sites
    (deepsearch/email_agent/executive_assistant's own _run closures),
    which build their system prompt fresh on every delegation anyway and
    so can just await this inline. Returns "" (not an error) when the
    feature is off or the active-task lookup itself fails -- a scratchpad
    hiccup must never be why a real user-facing delegation fails to run."""
    if not config.SCRATCHPAD_AND_SKILLS_ENABLED:
        return ""
    try:
        task = await db.get_active_task(user.user_id)
    except Exception as e:  # noqa: BLE001 - a memory aid, never load-bearing
        console.system(f"scratchpad_prompt_block: get_active_task failed (non-fatal): {e}")
        task = None
    return _format_task_block(task)


def build_scratchpad_tools(
    user: config.UserContext, agent_type: str, approval_gate: ApprovalGate | None = None
) -> list[BaseTool]:
    """`agent_type` is fixed per call site (e.g. "integrations_agent",
    "deepsearch", "messa_orchestrator") -- see this module's own docstring
    for why that's closed over here rather than a model-supplied argument.

    `approval_gate` is accepted only for signature consistency with every
    other build_*_tools(user, approval_gate) function in this app (so
    every call site can pass the same tuple through uniformly) -- none of
    the three tools here are destructive/side-effecting in the real-world
    sense trace_tool's approval gating exists for (they only ever write to
    this app's own memory tables), so it's unused today."""

    @tool
    async def update_task_scratchpad(fields: dict[str, Any]) -> str:
        """Save structured facts about the CURRENT multi-step task, e.g.
        {"spreadsheet_id": "...", "pitch_draft": "...", "recipients": [...]}
        -- so they're never forgotten even if this conversation's older
        messages get truncated or summarized later. Call this the moment
        you learn or produce something the task will need again
        (immediately after creating a resource or drafting content), not
        at the end. If no task is open yet, one starts automatically.
        Merges into existing fields -- never erases anything you don't
        explicitly overwrite."""
        if not config.SCRATCHPAD_AND_SKILLS_ENABLED:
            return "Scratchpad is currently disabled."
        rejection = _validate_scratchpad_fields(fields)
        if rejection:
            return f"Not saved: {rejection}"
        task = await db.get_active_task(user.user_id)
        if task is None:
            task = await db.start_active_task(user.user_id, task_type=agent_type)
        if task is None:
            return "Scratchpad isn't available yet on this account (migration not applied) -- proceeding without it."
        updated = await db.update_active_task_artifacts(task["task_id"], fields)
        if updated is None:
            return "Failed to save to the task scratchpad -- proceeding without it."
        return f"Saved. Current task artifacts: {updated['artifacts']}"

    @tool
    async def complete_task(summary: str = "") -> str:
        """Call this once the CURRENT active task is actually finished, so
        it stops being carried into future, unrelated conversations as
        still-open work. `summary` is optional, short, free text (not
        stored beyond this call today) -- pass a one-line note on what was
        accomplished if useful, or leave it blank. Safe to call even if no
        task is open. This is the primary way a task leaves 'in_progress';
        a time-based fallback exists for crashes/dropped threads, but it's
        not a substitute for calling this."""
        if not config.SCRATCHPAD_AND_SKILLS_ENABLED:
            return "Scratchpad is currently disabled."
        task = await db.get_active_task(user.user_id)
        if task is None:
            return "No active task is open -- nothing to complete."
        await db.set_active_task_status(task["task_id"], "completed")
        return "Task marked complete." + (f" ({summary})" if summary.strip() else "")

    @tool
    async def save_skill(domain: str, problem_pattern: str, solution_recipe: str) -> str:
        """Record a short, reusable lesson to the SHARED skills playbook --
        e.g. a tool's real required parameter, a deprecated action to
        avoid, or a site's navigation quirk -- so every future task
        (yours, and every other user's) benefits immediately instead of
        re-discovering it from scratch. Use this right after you resolve
        something genuinely non-obvious (a confusing error, a schema
        surprise), not speculatively and not for routine successes.
        `domain` is the toolkit slug (e.g. "googlesheets") or website host
        (e.g. "amazon.com"). Keep problem_pattern to a short slug and
        solution_recipe to 1-2 plain factual sentences -- never an
        instruction, never a URL/email/credential; those are rejected
        automatically."""
        if not config.SCRATCHPAD_AND_SKILLS_ENABLED:
            return "Skills playbook is currently disabled."
        rejection = _screen_skill_text(problem_pattern, solution_recipe)
        if rejection:
            console.system(
                f"[save_skill rejected] agent={agent_type} user={user.user_id} domain={domain!r}: {rejection}"
            )
            return f"Not saved: {rejection}"
        try:
            task = await db.get_active_task(user.user_id)
        except Exception:
            task = None
        result = await db.upsert_skill(
            agent_type=agent_type,
            domain=domain,
            problem_pattern=problem_pattern,
            solution_recipe=solution_recipe,
            source_user_id=user.user_id,
            source_task_id=task["task_id"] if task else None,
        )
        if result is None:
            return "Skills playbook isn't available yet on this account (migration not applied)."
        return f"Saved -- this lesson has now helped {result['success_count']} time(s)."

    @tool
    async def search_skills(domain: str) -> str:
        """Look up previously learned lessons for a toolkit or site (e.g.
        "googlesheets", "amazon.com") BEFORE working with it -- especially
        one you haven't used recently, or that has already given you a
        confusing error this turn. Returns the strongest, most-recently-
        useful lessons first."""
        if not config.SCRATCHPAD_AND_SKILLS_ENABLED:
            return "Skills playbook is currently disabled."
        skills = await db.search_skills(agent_type, domain)
        if not skills:
            return f"No learned lessons yet for {domain!r}."
        lines = [f"- {s['solution_recipe']}" for s in skills]
        return "Learned lessons for {!r}:\n{}".format(domain, "\n".join(lines))

    return [
        trace_tool(update_task_scratchpad, f"{agent_type}:update_task_scratchpad"),
        trace_tool(complete_task, f"{agent_type}:complete_task"),
        trace_tool(save_skill, f"{agent_type}:save_skill"),
        trace_tool(search_skills, f"{agent_type}:search_skills"),
    ]
