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

`build_email_subagent` (bottom of this file) is what actually registers
this as `email_agent` in agents/registry.py -- a `CompiledSubAgent` with
its own small step budget (config.EMAIL_RECURSION_LIMIT), same shape as
executive_tools.py's build_executive_subagent and for the same reason: a
plain declarative `SubAgent` dict has no way to give itself a step budget
of its own, so without this it silently inherited whatever recursion_limit
happened to be ambient on the call (config.RECURSION_LIMIT, 100 by
default) -- a research-sized budget for what's normally a small handful of
direct Composio calls. `build_email_tools` below is unchanged and still
usable directly (tests do exactly this); `build_email_subagent` just wraps
it in a bounded loop.
"""
from __future__ import annotations

import asyncio
import base64
import pathlib
from typing import Any, Optional

import httpx
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool

from .. import config, console, db, pdf_reader, reliability, usage
from ..approval import ApprovalGate
from ..channels.sendblue import SendblueError, send_message
from .common import last_ai_text, run_inner_agent_with_claim_check, trace_tool
from .integration_circuit_breaker import ToolFailureLadderMiddleware
from .scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block

LABEL = "email_agent"

# Composio action slugs for Gmail. If you connect Outlook instead, swap
# these for the OUTLOOK_* equivalents, update EMAIL_SYSTEM_PROMPT, and give
# _get_or_create_gmail_auth_config_id (below) an Outlook equivalent.
_SLUG_LIST = "GMAIL_FETCH_EMAILS"
_SLUG_GET = "GMAIL_FETCH_EMAILS"
_SLUG_GET_MESSAGE = "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
_SLUG_GET_ATTACHMENT = "GMAIL_GET_ATTACHMENT"
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
                "tools_for_connected_account_creation": [
                    _SLUG_LIST, _SLUG_SEND, _SLUG_REPLY, _SLUG_GET_ATTACHMENT, _SLUG_GET_MESSAGE,
                ],
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
            result = await _execute(_SLUG_GET_MESSAGE, message_id=message_id)
            if not isinstance(result, dict) or not result.get("successful"):
                result = await _execute(_SLUG_GET, query=f"id:{message_id}", max_results=1)
        except _NotConfigured as e:
            return str(e)
        except Exception:
            try:
                result = await _execute(_SLUG_GET, query=f"id:{message_id}", max_results=1)
            except Exception as e:
                return str(e)

        output = str(result)
        # Format attachment information cleanly if present
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict):
            att_list = data.get("attachmentList")
            if att_list is None and "messages" in data and isinstance(data["messages"], list) and data["messages"]:
                att_list = data["messages"][0].get("attachmentList")
            if att_list and isinstance(att_list, list):
                att_summaries = [
                    f"- {a.get('filename', 'attachment')} (attachmentId: {a.get('attachmentId')}, type: {a.get('mimeType', 'unknown')})"
                    for a in att_list
                    if isinstance(a, dict)
                ]
                if att_summaries:
                    output += (
                        "\n\nAttachments detected in this email:\n"
                        + "\n".join(att_summaries)
                        + "\nCall read_email_attachment(message_id=..., attachment_id=..., filename=...) to extract and read any attachment's text."
                    )
        return output

    @tool
    async def read_email_attachment(
        message_id: str,
        attachment_id: Optional[str] = None,
        filename: Optional[str] = None,
    ) -> str:
        """Read and extract text from an email attachment in Gmail (such as a PDF contract, report, or text document).

        message_id: the ID of the email containing the attachment (from list_recent_emails or get_email).
        attachment_id: optional specific attachment ID from the email's attachmentList. If omitted,
                       it will be automatically discovered from the email's attachments.
        filename: optional filename (e.g. 'contract.pdf') to match if the email has multiple attachments
                  or if attachment_id is omitted.
        """
        if not user.email_connected:
            return _not_connected_message()

        resolved_att_id = attachment_id
        resolved_filename = filename

        # If attachment_id or filename is missing, inspect the message to resolve them
        if not resolved_att_id or not resolved_filename:
            try:
                msg_res = await _execute(_SLUG_GET_MESSAGE, message_id=message_id)
                if not isinstance(msg_res, dict) or not msg_res.get("successful"):
                    msg_res = await _execute(_SLUG_GET, query=f"id:{message_id}", max_results=1)
            except _NotConfigured as e:
                return str(e)
            except Exception as e:
                return f"Could not fetch email details to find attachments: {e}"

            msg_data = msg_res.get("data") if isinstance(msg_res, dict) else {}
            att_list = []
            if isinstance(msg_data, dict):
                if "attachmentList" in msg_data and isinstance(msg_data["attachmentList"], list):
                    att_list = msg_data["attachmentList"]
                elif "messages" in msg_data and isinstance(msg_data["messages"], list) and msg_data["messages"]:
                    att_list = msg_data["messages"][0].get("attachmentList") or []

            if not att_list:
                return f"No attachments found in email message {message_id!r}."

            # If user provided a filename, try to find matching attachment
            target_att = None
            if resolved_filename:
                target_lower = resolved_filename.lower()
                for a in att_list:
                    fname = (a.get("filename") or "").lower()
                    if target_lower == fname or target_lower in fname or fname in target_lower:
                        target_att = a
                        break
                if not target_att:
                    available = ", ".join(a.get("filename") or "unknown" for a in att_list)
                    return (
                        f"Attachment {resolved_filename!r} was not found in email {message_id!r}. "
                        f"Available attachments: {available}"
                    )
            elif resolved_att_id:
                for a in att_list:
                    if a.get("attachmentId") == resolved_att_id:
                        target_att = a
                        break
                if not target_att:
                    available = ", ".join(a.get("attachmentId") or "unknown" for a in att_list)
                    return (
                        f"Attachment ID {resolved_att_id!r} was not found in email {message_id!r}. "
                        f"Available attachment IDs: {available}"
                    )
            else:
                # Neither provided: if only 1 attachment, pick it
                if len(att_list) == 1:
                    target_att = att_list[0]
                else:
                    # If multiple, prefer a PDF
                    pdf_candidates = [
                        a for a in att_list if (a.get("filename") or "").lower().endswith(".pdf")
                    ]
                    if len(pdf_candidates) == 1:
                        target_att = pdf_candidates[0]
                    else:
                        names = ", ".join(f"'{a.get('filename')}'" for a in att_list)
                        return (
                            f"Multiple attachments found in email {message_id!r}: {names}. "
                            "Please specify the filename to read."
                        )

            if target_att:
                resolved_att_id = target_att.get("attachmentId")
                resolved_filename = target_att.get("filename") or resolved_filename

        if not resolved_att_id:
            return f"Could not determine attachment ID for email {message_id!r}."
        if not resolved_filename:
            resolved_filename = "attachment.pdf"

        # Execute Composio GMAIL_GET_ATTACHMENT
        try:
            att_result = await _execute(
                _SLUG_GET_ATTACHMENT,
                message_id=message_id,
                attachment_id=resolved_att_id,
                file_name=resolved_filename,
            )
        except _NotConfigured as e:
            return str(e)
        except Exception as e:
            return f"Failed to retrieve attachment from Gmail: {e}"

        if not isinstance(att_result, dict) or not att_result.get("successful"):
            err_msg = att_result.get("error") if isinstance(att_result, dict) else str(att_result)
            return f"Gmail attachment retrieval was unsuccessful: {err_msg}"

        data = att_result.get("data") or {}
        file_info = data.get("file") if isinstance(data, dict) else {}
        if not isinstance(file_info, dict):
            file_info = {}

        # Fetch bytes from s3url, content, or path
        s3url = file_info.get("s3url") or (data.get("s3url") if isinstance(data, dict) else None)
        raw_bytes: bytes = b""

        if s3url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as http_client:
                    resp = await http_client.get(s3url)
                    resp.raise_for_status()
                    raw_bytes = resp.content
            except Exception as e:
                return f"Downloaded attachment link expired or failed to fetch: {e}"
        elif file_info.get("content"):
            try:
                raw_bytes = base64.b64decode(file_info["content"])
            except Exception as e:
                return f"Attachment content could not be base64-decoded: {e}"
        elif file_info.get("path"):
            try:
                raw_bytes = pathlib.Path(file_info["path"]).read_bytes()
            except Exception as e:
                return f"Could not read local attachment file at {file_info['path']}: {e}"

        if not raw_bytes:
            return f"Attachment {resolved_filename!r} could not be downloaded (empty response)."

        if len(raw_bytes) > config.MAX_PDF_READ_BYTES:
            limit_mb = config.MAX_PDF_READ_BYTES / (1024 * 1024)
            size_mb = len(raw_bytes) / (1024 * 1024)
            return (
                f"Attachment {resolved_filename!r} is too large to read "
                f"({size_mb:.1f}MB, system limit is {limit_mb:.0f}MB)."
            )

        # Check if PDF or text
        mimetype = str(file_info.get("mimetype") or "").lower()
        is_pdf = (
            resolved_filename.lower().endswith(".pdf")
            or raw_bytes.startswith(b"%PDF-")
            or "pdf" in mimetype
        )

        if is_pdf:
            try:
                text, pages_read, truncated = pdf_reader.extract_pdf_text(raw_bytes)
            except pdf_reader.PdfReadFailure as e:
                return f"Attachment {resolved_filename!r} arrived but couldn't be parsed as a PDF: {e}"

            trunc_note = (
                f" -- showing the first {pages_read} page(s), capped at {config.MAX_PDF_READ_PAGES}"
                if truncated else f" ({pages_read} page(s))"
            )
            return f"[Attachment: {resolved_filename}{trunc_note}]\n\n{text}"
        else:
            try:
                text = raw_bytes.decode("utf-8")
                return f"[Attachment: {resolved_filename}]\n\n{text}"
            except UnicodeDecodeError:
                return (
                    f"Attachment {resolved_filename!r} is a binary file ({len(raw_bytes)} bytes) "
                    "that cannot be parsed as plain text."
                )

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
    async def request_email_connection(switch_account: bool = False) -> str:
        """Generate a fresh Gmail OAuth connect link for this user and send it to them
        directly as its own text message right now. Call this when the user says
        something like 'connect my email/gmail', or during onboarding when they say yes
        to Messa managing their email. Do NOT try to relay the link yourself -- it's sent
        automatically; this tool's return value tells you what to say instead.

        switch_account: set True when the user explicitly wants to DISCONNECT
        their currently-connected Gmail and connect a different Google
        account instead (e.g. "switch my Gmail to my other account"). This
        actually disconnects the current one first (real, immediate:
        Composio's own connected_accounts.delete, not a manual step for the
        user) and then sends a fresh connect link, so the Google sign-in
        screen lets them pick a different account. There is no dashboard/
        settings page for the user to do this themselves -- this tool IS the
        disconnect. Leave this False for a normal first-time connect; if
        that raises "already connected" and the user didn't ask to switch,
        just tell them it's already set up rather than calling this again."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        if switch_account:
            existing = await db.get_active_email_connection(user.user_id)
            if existing and existing.get("connected_account_id"):

                def _delete_sync():
                    client.connected_accounts.delete(
                        existing["connected_account_id"], revoke_on_delete=True,
                    )

                try:
                    await asyncio.to_thread(_delete_sync)
                except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
                    return (
                        f"Couldn't disconnect the current Gmail connection to switch accounts: "
                        f"{e}. Tell the user it didn't work and to try again shortly -- don't "
                        "tell them to do it manually anywhere, there's no such page."
                    )
                await db.mark_email_disconnected(existing["id"], user.user_id)

        # max_connected_apps is a standing ceiling shared with
        # integration_tools.py's connect_integration_app, not a separate
        # Gmail-only cap -- Gmail is connected through this same
        # composio_user_id, so counting Composio's own live
        # connected_accounts here (not a cached/local number) already
        # includes it alongside every other connected toolkit.
        def _connected_count_sync() -> int:
            result = client.connected_accounts.list(user_ids=[composio_user_id], statuses=["ACTIVE"])
            items = getattr(result, "items", result)
            return len(list(items))

        live_count = await asyncio.to_thread(_connected_count_sync)
        cap_result = usage.check_connected_apps_cap(user, live_count)
        if not cap_result.allowed:
            return cap_result.upgrade_message

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
                "The user's Gmail is already connected. If they want to switch to a "
                "different Google account, call request_email_connection(switch_account=True) "
                "-- that disconnects the current one and sends a fresh link immediately, no "
                "manual step needed anywhere. Otherwise just let them know it's already set up."
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

    @tool
    async def disconnect_email() -> str:
        """Disconnect the user's currently-connected Gmail WITHOUT connecting
        a new one -- for when the user just wants it off, not switched. (For
        "switch to a different Google account", prefer
        request_email_connection(switch_account=True) instead -- one call,
        disconnect + fresh link together.) There's no dashboard/settings page
        for the user to do this themselves; this tool IS the disconnect,
        right now.

        Tells Composio to revoke the underlying OAuth grant too, but that
        revocation runs as Composio's own background job with no way for
        Messa to confirm it finished -- so say the disconnection is done
        (Messa's own side, and future requests, immediately stop using it),
        and that revoking the app's own access is in progress, not confirmed
        complete on the spot."""
        try:
            client = _get_client()
        except _NotConfigured as e:
            return str(e)

        existing = await db.get_active_email_connection(user.user_id)
        if not existing or not existing.get("connected_account_id"):
            return "The user's Gmail isn't currently connected -- nothing to disconnect."

        def _delete_sync():
            client.connected_accounts.delete(
                existing["connected_account_id"], revoke_on_delete=True,
            )

        try:
            await asyncio.to_thread(_delete_sync)
        except Exception as e:  # noqa: BLE001 - surfaced to the model, not raised
            return f"Couldn't disconnect Gmail: {e}. Tell the user it didn't work and to try again shortly."

        await db.mark_email_disconnected(existing["id"], user.user_id)
        return (
            "Done -- the user's Gmail is disconnected on Messa's side; she won't use it for "
            "anything going forward. Revoking access on Google's side is in progress in the "
            "background (not something you can confirm finished right now) -- tell them it's "
            "disconnected, and that fully revoking access may take a short moment on Google's "
            "end if they check there."
        )

    def _is_switch_account_call(*args: Any, **kwargs: Any) -> bool:
        """request_email_connection is only destructive on the branch that
        actually disconnects the current Gmail first -- a normal first-time
        connect (switch_account left False/default) still needs no
        confirmation, same as before this feature existed."""
        if "switch_account" in kwargs:
            return bool(kwargs["switch_account"])
        return len(args) > 0 and bool(args[0])

    def _always_destructive(*args: Any, **kwargs: Any) -> bool:
        return True

    raw_tools: list[BaseTool] = [
        list_recent_emails, get_email, read_email_attachment, send_email, reply_to_email,
        request_email_connection, check_email_connection_status, disconnect_email,
    ]

    def _destructive_check_for(name: str):
        if name == "request_email_connection":
            return _is_switch_account_call
        if name == "disconnect_email":
            return _always_destructive
        return None

    # outbound_emails is one logical usage-limits feature spanning three
    # physical send paths (see plans.py's own comment on the field) --
    # this is the Gmail leg of it. send_email and reply_to_email both
    # produce a real outbound message; get/list/status/disconnect tools
    # don't send anything and aren't tagged.
    _OUTBOUND_EMAIL_TOOLS = {"send_email", "reply_to_email"}

    return [
        trace_tool(
            t, LABEL,
            destructive=t.name in _DESTRUCTIVE,
            destructive_check=_destructive_check_for(t.name),
            approval_gate=approval_gate,
            feature="outbound_emails" if t.name in _OUTBOUND_EMAIL_TOOLS else None,
            user=user,
        )
        for t in raw_tools
    ]


def _build_system_prompt(user: config.UserContext) -> str:
    connection_str = (
        "Gmail IS connected -- reading/searching/sending/replying all work normally right now."
        if user.email_connected
        else (
            "Gmail is NOT connected yet -- every read/send tool below will tell you so plainly; "
            "relay that and offer request_email_connection instead of guessing at inbox contents."
        )
    )
    default_str = (
        " This is currently the user's DEFAULT inbox for a plain \"send/check my email\" "
        "request that doesn't name one -- Messa routes those to you directly."
        if user.default_email_provider == "gmail"
        else (
            " This is NOT currently the user's default inbox for an unnamed \"send/check my "
            "email\" request (that default is their own Messa address, personal_inbox_agent) "
            "-- Messa only delegates to you here because the user named Gmail/their personal "
            "email specifically, or is managing the Gmail connection itself."
        )
    )
    return (
        "You are the email specialist: you manage the user's own Gmail inbox via Composio, "
        "delegated to you by Messa.\n"
        f"{connection_str}{default_str}\n\n"
        "- Reading/searching email is safe to do freely, once connected. Call "
        "list_recent_emails first to find the message you need -- each result carries the "
        "message's OWN id and the id of the thread it's part of; use those ids for get_email/"
        "reply_to_email rather than guessing one you weren't just given.\n"
        "- Reading attachments: when an email contains attachments (indicated in get_email "
        "or list_recent_emails), call read_email_attachment(message_id=..., filename=...) to "
        "extract and read the text of the attachment (e.g. PDF contracts, documents, reports). "
        "You can specify the filename or attachment_id; if there is only one attachment, passing "
        "just the message_id is enough.\n"
        "- reply_to_email needs the THREAD id (not a single message's own id) -- Gmail threads "
        "the reply server-side once you pass the right one; if you only have a message id, use "
        "the thread id from that same result instead.\n"
        "- Sending or replying is irreversible and will prompt the user for confirmation before "
        "it goes out -- warn them plainly what you're about to send (to whom, roughly what it "
        "says) BEFORE the tool call, so the confirmation prompt isn't their first look at it.\n"
        "- If the user hasn't connected Gmail yet, the read/send tools will tell you so directly "
        "-- relay that plainly and offer request_email_connection, don't guess at their inbox.\n"
        "- Call request_email_connection when the user asks to connect their email/Gmail (or "
        "during onboarding, once they've said yes to Messa handling their email). It sends the "
        "connect link itself, as its own text -- never try to type out the URL yourself, and "
        "don't wait around for them to finish; just tell Messa you've sent it.\n"
        "- If the user wants to switch to a DIFFERENT Google account ('switch my gmail to my "
        "other account'), call request_email_connection(switch_account=True) -- it disconnects "
        "the current one and sends a fresh link in one step. If they just want Gmail "
        "disconnected with no reconnect, call disconnect_email() instead. There is no settings/"
        "integrations page for the user to do either of these themselves -- never say there is "
        "or tell them to do it manually; these tool calls ARE the disconnect, right now. Both "
        "confirm the disconnect on Messa's own side immediately, but the underlying Google-side "
        "revocation runs in the background -- say revocation is in progress, never that it's "
        "confirmed complete.\n"
        "- If a tool returns 'Email agent isn't configured yet', tell Messa so it can relay "
        "that the email channel needs setup, instead of pretending the action happened.\n"
        "- You have a small, bounded number of steps for this delegation -- if a call fails, "
        "report the error back to Messa rather than retrying the exact same call repeatedly.\n"
    )


def build_email_subagent(
    user: config.UserContext,
    model: BaseChatModel,
    approval_gate: ApprovalGate | None = None,
) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec -- same shape and same
    reasoning as executive_tools.py's build_executive_subagent: a plain
    declarative `SubAgent` dict (what email_agent used to be, registered
    straight in agents/registry.py) has no field for its own step budget,
    so it silently inherited whatever recursion_limit happened to be
    ambient on the call -- config.RECURSION_LIMIT (100 by default), the
    SAME research-sized budget deepsearch uses, for what's normally a
    small handful of direct Composio calls (list/get/send/reply, or the
    connect-link flow). Wrapping this as a CompiledSubAgent lets it set its
    own config.EMAIL_RECURSION_LIMIT on its own inner create_agent()
    invocation, independent of whatever the orchestrator's own limit is.

    Unlike executive_assistant, there's no deterministic post-hoc quality
    check run afterward here -- every read/send tool already checks
    user.email_connected itself and returns a correct, plain message when
    it isn't (there's no equivalent "past-due date" class of silent
    mistake to catch), so this is a single ainvoke with no checkpointer or
    nudge-retry machinery needed."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state["messages"])
        tools = build_email_tools(user, approval_gate)
        system_prompt = _build_system_prompt(user) + reliability.RELIABILITY_GUARDRAIL_STR
        # Active Task Scratchpad + Skills Playbook -- see tools/
        # scratchpad_tools.py's own module docstring; attached here (not by
        # registry.py) since this is a CompiledSubAgent that builds its own
        # inner agent fresh on every delegation.
        if config.SCRATCHPAD_AND_SKILLS_ENABLED:
            tools = tools + build_scratchpad_tools(user, "email_agent", approval_gate)
            system_prompt = system_prompt + await scratchpad_prompt_block(user)
        run_config = {"recursion_limit": config.EMAIL_RECURSION_LIMIT}
        inner_agent = create_agent(
            model=model, tools=tools, system_prompt=system_prompt,
            # "Rule of 3" self-healing (tools/integration_circuit_breaker.py)
            # -- email_agent had zero retry protection of any kind before
            # this, same as every subagent did before the reliability pass.
            middleware=[
                ToolFailureLadderMiddleware("send_email"),
                ToolFailureLadderMiddleware("reply_to_email"),
            ],
        )
        final_messages = await run_inner_agent_with_claim_check(
            inner_agent, messages, run_config, label="email_agent"
        )
        return {"messages": [AIMessage(content=last_ai_text(final_messages))]}

    return {
        "name": "email_agent",
        "description": (
            "Reads, searches, and sends the user's own Gmail (via Composio), reads email "
            "attachments (contracts, PDFs, documents), and handles connecting/reconnecting "
            "their account. Use for anything specifically about the user's Gmail inbox, "
            "for reading Gmail attachments, for 'connect my email/gmail' requests, and for "
            "a generic 'send/check my email' request when Gmail is currently the user's default inbox."
        ),
        "runnable": RunnableLambda(_run),
    }
