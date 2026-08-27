"""Assembles Messa (the orchestrator) and her five subagents.

Uses deepagents' native `subagents=` support on `create_deep_agent`, so
delegation, the `task` tool, and per-subagent tool/prompt isolation are all
provided by the framework rather than hand-rolled. Each subagent here is a
plain declarative `SubAgent` dict: name + description (what makes Messa pick
it) + system_prompt + its own tool list.

Confirming or rejecting a *proposed* action (see db.py's pending_actions
gate) is deliberately kept OFF every subagent and given to Messa directly --
subagents can suggest a mutation, only the orchestrator holding the actual
conversation with the user can commit it.
"""
from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool
from deepagents import create_deep_agent

from .. import config, db
from ..approval import ApprovalGate, CLIApprovalGate
from ..tools.common import trace_all
from ..tools.browser_tools import BROWSER_SYSTEM_PROMPT
from ..tools.executive_tools import build_executive_tools, EXECUTIVE_SYSTEM_PROMPT
from ..tools.email_tools import build_email_tools, EMAIL_SYSTEM_PROMPT
from ..tools.document_tools import build_document_tools, DOCUMENT_SYSTEM_PROMPT
from ..tools.routines_tools import build_routines_tools, ROUTINES_SYSTEM_PROMPT

ORCHESTRATOR_LABEL = "messa"


def build_orchestrator_tools(user: config.UserContext) -> list[BaseTool]:
    """Messa's own direct tools: approvals + lightweight project tracking."""
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

    raw_tools: list[BaseTool] = [
        confirm_pending_action, reject_pending_action, list_pending_actions,
        track_project, list_active_projects,
    ]
    return trace_all(raw_tools, ORCHESTRATOR_LABEL)


ORCHESTRATOR_SYSTEM_PROMPT = (
    "You are Messa, a personal assistant reachable by text, email, and (soon) WhatsApp. "
    "You talk to the user directly and delegate specialized work to subagents via the "
    "task tool: browser_agent (web browsing/research), executive_assistant (tasks, "
    "reminders, notes, contacts, calendar), email_agent (the user's own inbox), "
    "document_agent (generates PDFs), routines_agent (recurring automations).\n\n"
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
    browser_tools: list[BaseTool],
    approval_gate: ApprovalGate | None = None,
    model: Any = None,
) -> Any:
    approval_gate = approval_gate or CLIApprovalGate()
    model = model or config.build_model()

    subagents = [
        {
            "name": "browser_agent",
            "description": (
                "Performs live web browsing: navigating sites, reading pages, filling "
                "forms, clicking through flows, and reporting back what it found/did. "
                "Use for anything that requires actually visiting a website."
            ),
            "system_prompt": BROWSER_SYSTEM_PROMPT,
            "tools": browser_tools,
        },
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
        system_prompt=ORCHESTRATOR_SYSTEM_PROMPT,
        subagents=subagents,
    )
    return agent
