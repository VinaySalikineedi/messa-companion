"""Email subagent: manages the user's own inbox (Gmail, for now) via Composio,
including the OAuth connection flow itself -- generating a per-user Gmail
connect link, texting it to them directly, and tracking when it goes live.

Real multi-tenant setup: every Composio call is keyed by that specific
user's own id (`_composio_user_id`), not a single shared connected account
-- each user connects (and can later disconnect) their own Gmail.

Verified against the actual installed `composio` SDK (0.21.0) and
`composio-client` (1.43.0) package sources, not just Composio's docs (which
turned out to describe more than one still-current API shape for the same
operation) -- specifically:
  - `connected_accounts.link(user_id, auth_config_id, callback_url=...)` is
    used to start the OAuth flow, NOT `.initiate()`: reading the SDK source
    directly showed `.initiate()` is mid-retirement for exactly this case
    (Composio-managed auth on a redirectable OAuth scheme like Gmail) on a
    rolling cutover between 2026-05-08 and 2026-07-03 -- today is well past
    that window, so `.initiate()` may already be dead for this org.
    `.link()` returns the same shape and isn't affected.
  - The Gmail OAuth scopes granted aren't hand-picked Google scope strings;
    `auth_configs.create`'s `tool_access_config.tools_for_connected_account_creation`
    lets Composio compute the *minimum* scopes needed for a specific list of
    actions. `_get_or_create_gmail_auth_config_id` passes exactly the four
    Gmail action slugs this file calls (read/search/get/send/reply) -- i.e.
    read + write, not a blanket full-account grant, matching what was asked
    for.
  - Every Composio SDK call in this file is synchronous/blocking (it's a
    plain `requests`-based client) -- including the *previous* version of
    this file's `_execute`, which called it directly inside `async def`
    tool functions. That would block the whole asyncio event loop (i.e.
    every other user's turn, and the FastAPI webhook handler itself) for
    however long the Gmail API call takes. Every blocking call in this file
    now goes through `asyncio.to_thread`.

Send/reply are treated as destructive (confirmed via the ApprovalGate, same
mechanism as deepsearch's destructive actions) since they're irreversible
and not covered by the DB's pending_actions gate. Connecting/checking the
connection are not destructive -- nothing is sent or changed, only a link
generated or a status read.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from langchain_core.tools import BaseTool, tool

from .. import config, console, db
from ..approval import ApprovalGate
from ..channels.sendblue import SendblueError, send_message
from .common import trace_tool

LABEL = "email_agent"

# Composio action slugs for Gmail. If you connect Outlook instead, swap
# these for the OUTLOOK_* equivalents, update EMAIL_SYSTEM_PROMPT, and give
# _get_or_create_gmail_auth_config_id (below) an Outlook equivalent.
_SLUG_LIST = "GMAIL_FETCH_EMAILS"
_SLUG_SEND = "GMAIL_SEND_EMAIL"
_SLUG_REPLY = "GMAIL_REPLY_TO_THREAD"

_DESTRUCTIVE = {"send_email", "reply_to_email"}

# Fixed name so _get_or_create_gmail_auth_config_id can find (and reuse) the
# auth config it created on an earlier process/deploy, rather than either
# creating a new one every cold start or accidentally adopting some
# unrelated Gmail auth config already sitting in the same Composio project.
_GMAIL_AUTH_CONFIG_NAME = "Messa Gmail Access v2"

# In-process memoization only -- see _get_or_create_gmail_auth_config_id's
# docstring for why nothing needs to persist this to the DB for it to
# survive a restart.
_gmail_auth_config_id_cache: str | None = None


class _NotConfigured(Exception):
    pass


def _get_client():
    if not config.COMPOSIO_API_KEY:
        raise _NotConfigured(
            "Email agent isn't configured yet -- add COMPOSIO_API_KEY to .env to enable it."
        )
    from composio import Composio  # imported lazily so the app runs without composio installed

    return Composio(api_key=config.COMPOSIO_API_KEY)


def _composio_user_id(user: config.UserContext) -> str:
    """Composio's own per-user identifier -- a stable internal id rather
    than the phone number, so it survives a user changing numbers and
    doesn't put PII into Composio's records unnecessarily."""
    return str(user.user_id)


def _get_or_create_gmail_auth_config_id(client) -> str:
    """Find-or-create, memoized for this process: the one Composio auth
    config every user's Gmail connects through.

    Deliberately scoped to exactly the Gmail actions this file calls
    (see module docstring) via tool_access_config, so Composio computes the
    minimum OAuth scopes for those specifically -- not a blanket
    full-account grant.

    Reused across restarts without this project needing to persist the id
    itself: every cache-miss call lists existing Gmail auth configs and
    reuses the one matching _GMAIL_AUTH_CONFIG_NAME (created by this same
    function on some earlier run), so a redeploy doesn't spawn a fresh auth
    config -- and fresh OAuth scopes/consent screen -- every time.
    config.COMPOSIO_GMAIL_AUTH_CONFIG_ID overrides this entirely (e.g. to
    point at your own custom Gmail OAuth app instead of Composio's managed
    one).

    Blocking (plain `requests` calls under the hood) -- always call this
    via asyncio.to_thread, never directly from an async tool function.
    """
    global _gmail_auth_config_id_cache
    if config.COMPOSIO_GMAIL_AUTH_CONFIG_ID:
        return config.COMPOSIO_GMAIL_AUTH_CONFIG_ID
    if _gmail_auth_config_id_cache:
        return _gmail_auth_config_id_cache

    existing = client.auth_configs.list(toolkit_slug="gmail")
    for item in existing.items:
        if item.name == _GMAIL_AUTH_CONFIG_NAME:
            _gmail_auth_config_id_cache = item.id
            return item.id

    created = client.auth_configs.create(
        "gmail",
        {
            "type": "use_composio_managed_auth",
            "name": _GMAIL_AUTH_CONFIG_NAME,
            "tool_access_config": {
                "tools_for_connected_account_creation": [_SLUG_LIST, _SLUG_SEND, _SLUG_REPLY],
            },
        },
    )
    _gmail_auth_config_id_cache = created.id
    console.system(
        f"Composio: created Gmail auth config {created.id!r}, scoped to read/search/send/reply "
        "only. Optional: set COMPOSIO_GMAIL_AUTH_CONFIG_ID to this value in your env to skip this "
        "lookup on future cold starts."
    )
    return created.id


async def get_connection_status(connected_account_id: str) -> str | None:
    """Composio's current status string for one connected account (e.g.
    'ACTIVE', 'INITIALIZING', 'FAILED', 'EXPIRED', 'REVOKED'), or None if
    Composio isn't configured or the lookup itself failed. This is the one
    piece of Composio-specific knowledge server.py's background poll loop
    needs (see _production_email_connection_poll_loop) -- kept here rather
    than duplicated there, same reasoning as everything else in this file:
    the SDK call is blocking, so it always goes through asyncio.to_thread."""
    try:
        client = _get_client()
    except _NotConfigured:
        return None

    def _get():
        return client.connected_accounts.get(connected_account_id)

    try:
        account = await asyncio.to_thread(_get)
    except Exception as e:  # noqa: BLE001 - the poll loop just retries next cycle
        console.system(f"Composio: connection status lookup failed for {connected_account_id!r}: {e}")
        return None
    return account.status


def build_email_tools(user: config.UserContext, approval_gate: ApprovalGate | None = None) -> list[BaseTool]:
    composio_user_id = _composio_user_id(user)

    def _execute_sync(slug: str, **arguments) -> dict:
        client = _get_client()
        kwargs: dict = {"slug": slug, "arguments": arguments, "user_id": composio_user_id}
        if config.COMPOSIO_TOOLKIT_VERSION:
            kwargs["version"] = config.COMPOSIO_TOOLKIT_VERSION
        else:
            kwargs["dangerously_skip_version_check"] = True
        return client.tools.execute(**kwargs)

    async def _execute(slug: str, **arguments) -> dict:
        return await asyncio.to_thread(_execute_sync, slug, **arguments)

    def _not_connected_message() -> str:
        return (
            "The user hasn't connected their Gmail yet. Tell them so and offer to send a "
            "connect link with request_email_connection -- don't guess at their inbox contents."
        )

    @tool
    async def list_recent_emails(max_results: int = 10, query: Optional[str] = None) -> str:
        """List/search the user's recent emails. query is an optional Gmail search string
        (e.g. 'is:unread', 'from:someone@example.com')."""
        if not user.email_connected:
            return _not_connected_message()
        try:
            result = await _execute(_SLUG_LIST, max_results=max_results, query=query or "")
        except _NotConfigured as e:
            return str(e)
        return str(result)

    @tool
    async def get_email(message_id: str) -> str:
        """Fetch the full content of one email by its message id."""
        if not user.email_connected:
            return _not_connected_message()
        try:
            result = await _execute(_SLUG_GET, message_id=message_id)
        except _NotConfigured as e:
            return str(e)
        return str(result)

    @tool
    async def send_email(to: str, subject: str, body: str) -> str:
        """Send a new email. Irreversible -- requires user confirmation."""
        if not user.email_connected:
            return _not_connected_message()
        try:
            result = await _execute(_SLUG_SEND, recipient_email=to, subject=subject, body=body)
        except _NotConfigured as e:
            return str(e)
        return str(result)

    @tool
    async def reply_to_email(thread_id: str, body: str) -> str:
        """Reply within an existing email thread. Irreversible -- requires user confirmation."""
        if not user.email_connected:
            return _not_connected_message()
        try:
            result = await _execute(_SLUG_REPLY, thread_id=thread_id, message_body=body)
        except _NotConfigured as e:
            return str(e)
        return str(result)

    @tool
    async def request_email_connection() -> str:
        """Generate a fresh Gmail OAuth connect link for this user and send it to them
        directly as its own text message right now. Call this when the user says
        something like 'connect my email/gmail', or during onboarding when they say yes
        to Messa managing their email. Do NOT try to relay the link yourself -- it's sent
        automatically; this tool's return value tells you what to say instead."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        def _start_link():
            auth_config_id = _get_or_create_gmail_auth_config_id(client)
            return client.connected_accounts.link(
                user_id=composio_user_id,
                auth_config_id=auth_config_id,
                callback_url=config.COMPOSIO_GMAIL_CALLBACK_URL,
            )

        from composio import exceptions as composio_exceptions

        try:
            connection_request = await asyncio.to_thread(_start_link)
        except composio_exceptions.ComposioMultipleConnectedAccountsError:
            return (
                "The user's Gmail is already connected -- there's no need to send another "
                "link. Just let them know it's already set up."
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return f"Couldn't start the Gmail connection: {e}"

        await db.create_email_connection_request(user.user_id, connection_request.id)

        link = connection_request.redirect_url
        if not link:
            return (
                "Composio didn't return a connect link -- something's misconfigured "
                "(check COMPOSIO_API_KEY and the Gmail auth config). Tell the user it "
                "didn't work and to try again shortly."
            )

        if user.channel == "cli":
            # No real phone to text in local/dev use -- surface the link directly
            # instead, same as how the CLI's own console tracing shows everything.
            return f"Connect link (CLI/dev mode -- would normally be texted directly): {link}"

        try:
            await send_message(user.phone_number, f"Connect your Gmail here: {link}")
        except SendblueError as e:
            return (
                f"Generated the connect link but couldn't text it (Sendblue error: {e}). "
                "Tell the user to ask again in a moment."
            )
        return (
            "Sent. The Gmail connect link just went out to the user as its own text message -- "
            "don't repeat the URL yourself. Just tell them to check their messages, and that "
            "you'll let them know once it's connected."
        )

    @tool
    async def check_email_connection_status() -> str:
        """Check whether the user's Gmail is connected right now. Use this if they ask
        'is my email connected yet?' instead of waiting for the automatic confirmation
        that arrives once the connection finishes."""
        if user.email_connected:
            return "Gmail is connected."
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        def _check():
            auth_config_id = _get_or_create_gmail_auth_config_id(client)
            return client.connected_accounts.list(
                user_ids=[composio_user_id], auth_config_ids=[auth_config_id], statuses=["ACTIVE"],
            )

        try:
            result = await asyncio.to_thread(_check)
        except Exception as e:  # noqa: BLE001
            return f"Couldn't check the connection status: {e}"
        if result.items:
            return (
                "Gmail is actually connected now (the cached flag just hasn't caught up yet -- "
                "that's fine, it'll self-correct shortly)."
            )
        return "Gmail isn't connected yet -- offer to send a connect link with request_email_connection."

    raw_tools: list[BaseTool] = [
        list_recent_emails, get_email, send_email, reply_to_email,
        request_email_connection, check_email_connection_status,
    ]
    return [
        trace_tool(t, LABEL, destructive=t.name in _DESTRUCTIVE, approval_gate=approval_gate)
        for t in raw_tools
    ]


EMAIL_SYSTEM_PROMPT = (
    "You are the email specialist: you manage the user's own Gmail inbox via Composio, "
    "delegated to you by Messa.\n"
    "- Reading/searching email is safe to do freely, once connected.\n"
    "- Sending or replying is irreversible and will prompt the user for confirmation before "
    "it goes out -- warn them what you're about to send first.\n"
    "- If the user hasn't connected Gmail yet, the read/send tools will tell you so directly "
    "-- relay that plainly and offer request_email_connection, don't guess at their inbox.\n"
    "- Call request_email_connection when the user asks to connect their email/Gmail (or "
    "during onboarding, once they've said yes to Messa handling their email). It sends the "
    "connect link itself, as its own text -- never try to type out the URL yourself, and "
    "don't wait around for them to finish; just tell Messa you've sent it.\n"
    "- If a tool returns 'Email agent isn't configured yet', tell Messa so it can relay "
    "that the email channel needs setup, instead of pretending the action happened.\n"
)
