"""Assembles Messa (the orchestrator) and her five subagents.

Uses deepagents' native `subagents=` support on `create_deep_agent`, so
delegation, the `task` tool, and per-subagent tool/prompt isolation are all
provided by the framework rather than hand-rolled. Four subagents are plain
declarative `SubAgent` dicts: name + description (what makes Messa pick it)
+ system_prompt + its own tool list. deepsearch (the browsing/research
subagent, formerly called "browser_agent") is a `CompiledSubAgent` instead
-- see tools/deepsearch_tools.py for why (it needs to open/close its own
Playwright process per delegation, and persists/resumes progress across
runs that hit their step limit).

Confirming or rejecting a *proposed* action (see db.py's pending_actions
gate) is deliberately kept OFF every subagent and given to Messa directly --
subagents can suggest a mutation, only the orchestrator holding the actual
conversation with the user can commit it.

Latency: `create_deep_agent`'s default harness gives every agent a virtual
filesystem (ls/read_file/write_file/edit_file/delete/glob/grep), a shell
`execute` tool, and an auto-added "general-purpose" subagent -- none of
which this project uses. Every one of those is extra tool-schema tokens on
every single model call, which adds up across 6 agents. `register_harness_profile`
below strips them once, globally, for every agent built on our model.
"""
from __future__ import annotations

from typing import Any

from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
from langchain_core.tools import BaseTool, tool

from .. import config, db
from ..approval import ApprovalGate, CLIApprovalGate
from ..tools.deepsearch_tools import build_deepsearch_subagent
from ..tools.common import trace_all
from ..tools.document_tools import DOCUMENT_SYSTEM_PROMPT, build_document_tools
from ..tools.email_tools import EMAIL_SYSTEM_PROMPT, build_email_tools
from ..tools.executive_tools import EXECUTIVE_SYSTEM_PROMPT, build_executive_tools
from ..tools.routines_tools import ROUTINES_SYSTEM_PROMPT, build_routines_tools

ORCHESTRATOR_LABEL = "messa"

# Registered once at import time. All our agents use the same ChatOpenAI
# instance pointed at OpenRouter, which deepagents' model-introspection
# resolves to provider "openai" regardless of the actual model string (see
# deepagents._models.get_model_provider) -- so a provider-wide registration
# here covers Messa and every subagent built via create_deep_agent.
register_harness_profile(
    "openai",
    HarnessProfile(
        excluded_tools=frozenset({"ls", "read_file", "write_file", "edit_file", "delete", "glob", "grep", "execute"}),
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    ),
)


def build_orchestrator_tools(user: config.UserContext) -> list[BaseTool]:
    """Messa's own direct tools: approvals, lightweight project tracking, onboarding."""
    uid = user.user_id

    @tool
    async def confirm_pending_action(pending_action_id: int) -> str:
        """Apply a previously-proposed action now that the user has confirmed it."""
        result = await db.confirm_pending_action(uid, pending_action_id)
        if not result.get("ok"):
            return f"Could not confirm #{pending_action_id}: {result.get('error')}"
        return f"Confirmed and applied action #{pending_action_id} ({result['action_type']})."

    @tool
    async def reject_pending_action(pending_action_id: int) -> str:
        """Discard a previously-proposed action the user declined."""
        result = await db.reject_pending_action(uid, pending_action_id)
        if not result.get("ok"):
            return f"Could not reject #{pending_action_id}: {result.get('error')}"
        return f"Rejected action #{pending_action_id}. Nothing was changed."

    @tool
    async def list_pending_actions() -> str:
        """List actions awaiting the user's confirmation."""
        rows = await db.list_pending_actions(uid)
        if not rows:
            return "Nothing pending confirmation."
        return "\n".join(f"#{r['id']} {r['action_type']}: {r['payload']}" for r in rows)

    @tool
    async def track_project(title: str) -> str:
        """Get-or-create a lightweight project thread for a multi-step user request,
        so related tasks/messages can be grouped together. Safe to call even if the
        projects table/migration hasn't been applied -- it will just no-op."""
        row = await db.get_or_create_project(uid, title)
        if row is None:
            return "Project tracking isn't enabled yet (run migrations/002_projects_and_channel.sql)."
        return f"Tracking project '{row['title']}' (#{row['id']})."

    @tool
    async def list_active_projects() -> str:
        """List the user's active tracked projects."""
        rows = await db.list_projects(uid)
        if not rows:
            return "No active projects."
        return "\n".join(f"#{r['id']} {r['title']}" for r in rows)

    @tool
    async def save_profile_info(field: str, value: str) -> str:
        """Save one onboarding field the user just told you: field is 'name', 'email',
        or 'city'. For the optional email step, pass value='skip' if the user doesn't
        want to share one. This also advances onboarding to the next question."""
        row = await db.save_profile_field(uid, field, value)
        return f"Saved {field}. Onboarding is now at: {row['onboarding_step']}."

    @tool
    async def list_deepsearch_sessions(status: str | None = None) -> str:
        """List the user's deepsearch (browsing/research) sessions, most recent first.
        status: 'active' (unfinished/resumable), 'completed', or omit for all. Use this
        to check whether an earlier research task is still in progress before starting a
        new one, or when the user asks what you were looking into."""
        rows = await db.list_deepsearch_sessions(uid, status)
        if not rows:
            return "No deepsearch sessions found."
        return "\n".join(
            f"#{r['id']} [{r['status']}] {r['title']} (updated {r['updated_at']}): "
            f"{(r['summary'] or '')[:140]}"
            for r in rows
        )

    raw_tools: list[BaseTool] = [
        confirm_pending_action, reject_pending_action, list_pending_actions,
        track_project, list_active_projects, save_profile_info, list_deepsearch_sessions,
    ]
    return trace_all(raw_tools, ORCHESTRATOR_LABEL)


_ONBOARDING_PROMPTS = {
    "awaiting_name": (
        "This is a brand-new user and you don't know their name yet. Before diving into "
        "their first request (or right after helping with it if they jumped straight to a "
        "task), casually ask what you should call them, then call save_profile_info('name', ...)."
    ),
    "awaiting_email": (
        "You know the user's name but not their email. Casually ask for it once (mention "
        "it's optional / skippable), then call save_profile_info('email', ...) with what "
        "they give you, or save_profile_info('email', 'skip') if they'd rather not share it."
    ),
    "awaiting_location": (
        "You still need the user's general location (city is enough) so results like "
        "weather/local search/timezone-aware scheduling are accurate. Ask casually, then "
        "call save_profile_info('city', ...)."
    ),
}


def _build_system_prompt(user: config.UserContext) -> str:
    known = []
    if user.name:
        known.append(f"name: {user.name}")
    if user.email:
        known.append(f"email: {user.email}")
    if user.city:
        known.append(f"city: {user.city}")
    known_str = ("Known about this user so far -- " + ", ".join(known) + ".\n\n") if known else ""

    onboarding_str = ""
    if not user.onboarding_complete:
        instruction = _ONBOARDING_PROMPTS.get(user.onboarding_step)
        if instruction:
            onboarding_str = instruction + "\n\n"

    channel_str = ""
    if user.channel != "cli":
        channel_str = (
            f"You're replying over {user.channel} (a real text conversation, not a chat "
            "UI). Write plain text only -- no markdown (no **bold**, no bullet/numbered "
            "lists, no headers, no code fences). Use line breaks and casual punctuation "
            "the way a person texting would. Keep it as short as the answer allows.\n\n"
        )

    return (
        "You are Messa, a personal assistant reachable by text, email, and (soon) WhatsApp. "
        "You talk to the user directly and delegate specialized work to subagents via the "
        "task tool: deepsearch (web browsing/research), executive_assistant (tasks, "
        "reminders, notes, contacts, calendar), email_agent (the user's own inbox), "
        "document_agent (generates PDFs), routines_agent (recurring automations).\n\n"
        f"{known_str}"
        f"{onboarding_str}"
        f"{channel_str}"
        "Responsiveness: delegating to a subagent can take a little while. Before calling "
        "the task tool, send one short line acknowledging what you're about to do (e.g. "
        "\"Checking flights now...\") so the user isn't staring at silence -- don't just go "
        "straight to a silent tool call.\n\n"
        "Live data means a fresh check, every time: if the user asks for anything that can "
        "change between messages -- a price, availability, a live status, today's weather -- "
        "and they're asking again (even just \"try again\" or \"is it back yet\"), delegate to "
        "deepsearch again. Never answer from what a subagent told you on an earlier turn for "
        "this kind of request, even if that earlier attempt failed and you're confident the "
        "failure is still true -- conditions on your end (the browser, the network, an "
        "external service) can change silently between messages, and telling the user "
        "'it hasn't changed since I last checked' without actually checking again is a "
        "worse failure than a slow reply.\n\n"
        "Deepsearch sessions: a deepsearch reply always starts with "
        "'[deepsearch session #<id> -- completed]' or '...-- not finished, hit its step "
        "limit'. When it's not finished and the task is still worth continuing, delegate "
        "again with 'session #<id>' written in your description (e.g. \"session #12: also "
        "check the return flights\") so it resumes with everything it already found instead "
        "of starting over. Use list_deepsearch_sessions if you need to check on or remind "
        "yourself of past research before starting something that might duplicate it.\n\n"
        "Confirmation flow: executive_assistant and routines_agent can only PROPOSE "
        "creating/updating/deleting things -- they report back a pending id. You must "
        "relay that proposal to the user in plain language and get an explicit yes before "
        "calling confirm_pending_action; call reject_pending_action if they decline or the "
        "details need to change. Never call confirm_pending_action without the user having "
        "just said yes to that specific thing.\n\n"
        "Use track_project when a request looks like it'll span multiple turns or tasks "
        "(e.g. planning a trip, redesigning something), so related work stays grouped.\n\n"
        "Be concise -- responses may be read as a text message. Don't restate a subagent's "
        "full output verbatim; summarize what matters to the user.\n"
    )


async def build_orchestrator(
    user: config.UserContext,
    approval_gate: ApprovalGate | None = None,
    model: Any = None,
) -> Any:
    approval_gate = approval_gate or CLIApprovalGate()
    model = model or config.build_model()

    subagents = [
        build_deepsearch_subagent(user, model, approval_gate),
        {
            "name": "executive_assistant",
            "description": (
                "Manages tasks, reminders, notes, contacts, and calendar events. Use for "
                "anything about the user's to-dos, schedule, or personal notes/contacts."
            ),
            "system_prompt": EXECUTIVE_SYSTEM_PROMPT,
            "tools": build_executive_tools(user),
        },
        {
            "name": "email_agent",
            "description": (
                "Reads, searches, and sends the user's own email (Gmail/Outlook via "
                "Composio). Use for anything about the user's inbox."
            ),
            "system_prompt": EMAIL_SYSTEM_PROMPT,
            "tools": build_email_tools(user, approval_gate),
        },
        {
            "name": "document_agent",
            "description": "Generates polished PDF documents from structured content on request.",
            "system_prompt": DOCUMENT_SYSTEM_PROMPT,
            "tools": build_document_tools(),
        },
        {
            "name": "routines_agent",
            "description": (
                "Sets up, lists, pauses, resumes, and cancels recurring automations "
                "(cron-scheduled reminders/tasks). Use for anything recurring/scheduled."
            ),
            "system_prompt": ROUTINES_SYSTEM_PROMPT,
            "tools": build_routines_tools(user),
        },
    ]

    agent = create_deep_agent(
        model=model,
        tools=build_orchestrator_tools(user),
        system_prompt=_build_system_prompt(user),
        subagents=subagents,
    )
    return agent
