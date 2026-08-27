"""Email subagent: manages the user's own inbox (Gmail/Outlook) via Composio.

IMPORTANT / not yet verified end-to-end: this was written without a live
Composio API key or connected account (none was available while building
Phase 1), so the exact tool-call shape below is based on Composio's current
documented client API (`Composio(api_key=...)`, `.tools.execute(slug=...,
arguments=..., user_id=...)`) rather than a tested call. Once you add
COMPOSIO_API_KEY (and connect your Gmail account through Composio's normal
OAuth flow) to .env, sanity-check the action slugs below against
https://docs.composio.dev for your account's Composio SDK version --
slugs/params occasionally change between SDK majors.

Send/reply are treated as destructive (confirmed via the CLI ApprovalGate,
same mechanism as the browser agent's destructive actions) since they're
irreversible and not covered by the DB's pending_actions gate.
"""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import BaseTool, tool

from .. import config
from ..approval import ApprovalGate
from .common import trace_tool

LABEL = "email_agent"

# Composio action slugs for Gmail. If you connect Outlook instead, swap
# these for the OUTLOOK_* equivalents and update EMAIL_SYSTEM_PROMPT.
_SLUG_LIST = "GMAIL_FETCH_EMAILS"
_SLUG_GET = "GMAIL_GET_MESSAGE"
_SLUG_SEND = "GMAIL_SEND_EMAIL"
_SLUG_REPLY = "GMAIL_REPLY_TO_THREAD"

_DESTRUCTIVE = {"send_email", "reply_to_email"}


class _NotConfigured(Exception):
    pass


def _get_client():
    if not config.COMPOSIO_API_KEY:
        raise _NotConfigured(
            "Email agent isn't configured yet -- add COMPOSIO_API_KEY (and connect an "
            "account) to .env to enable it."
        )
    from composio import Composio  # imported lazily so the app runs without composio installed

    return Composio(api_key=config.COMPOSIO_API_KEY)


def build_email_tools(user: config.UserContext, approval_gate: ApprovalGate | None = None) -> list[BaseTool]:
    composio_user_id = config.COMPOSIO_EMAIL_ACCOUNT or str(user.phone_number)

    def _execute(slug: str, **arguments) -> dict:
        client = _get_client()
        return client.tools.execute(slug=slug, arguments=arguments, user_id=composio_user_id)

    @tool
    async def list_recent_emails(max_results: int = 10, query: Optional[str] = None) -> str:
        """List/search the user's recent emails. query is an optional Gmail search string
        (e.g. 'is:unread', 'from:someone@example.com')."""
        try:
            result = _execute(_SLUG_LIST, max_results=max_results, query=query or "")
        except _NotConfigured as e:
            return str(e)
        return str(result)

    @tool
    async def get_email(message_id: str) -> str:
        """Fetch the full content of one email by its message id."""
        try:
            result = _execute(_SLUG_GET, message_id=message_id)
        except _NotConfigured as e:
            return str(e)
        return str(result)

    @tool
    async def send_email(to: str, subject: str, body: str) -> str:
        """Send a new email. Irreversible -- requires user confirmation."""
        try:
            result = _execute(_SLUG_SEND, recipient_email=to, subject=subject, body=body)
        except _NotConfigured as e:
            return str(e)
        return str(result)

    @tool
    async def reply_to_email(thread_id: str, body: str) -> str:
        """Reply within an existing email thread. Irreversible -- requires user confirmation."""
        try:
            result = _execute(_SLUG_REPLY, thread_id=thread_id, message_body=body)
        except _NotConfigured as e:
            return str(e)
        return str(result)

    raw_tools: list[BaseTool] = [list_recent_emails, get_email, send_email, reply_to_email]
    return [
        trace_tool(t, LABEL, destructive=t.name in _DESTRUCTIVE, approval_gate=approval_gate)
        for t in raw_tools
    ]


EMAIL_SYSTEM_PROMPT = (
    "You are the email specialist: you manage the user's own Gmail inbox via Composio, "
    "delegated to you by Messa.\n"
    "- Reading/searching email is safe to do freely.\n"
    "- Sending or replying is irreversible and will prompt the user for confirmation before "
    "it goes out -- warn them what you're about to send first.\n"
    "- If any tool returns 'Email agent isn't configured yet', tell Messa so it can relay "
    "that the email channel needs setup, instead of pretending the action happened.\n"
)
