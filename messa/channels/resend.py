"""Thin async client for Resend (https://resend.com) -- outbound half of
Messa's own personal-inbox email, sent from each user's own
<local-part>@config.TEXTMESSA_EMAIL_DOMAIN address (see
tools/personal_inbox_tools.py). This is NOT the user's personal Gmail --
that's a completely separate system (tools/email_tools.py, via Composio
OAuth). Inbound for this same address is a different pipeline too:
Cloudflare Email Routing -> cloudflare/personal-email-worker/ -> server.py's
/webhooks/personal-email/inbound.

One endpoint, no SDK -- same reasoning as channels/sendblue.py: httpx is
already a dependency, and Resend's send API is a single POST with a bearer
token, so a whole SDK isn't worth the extra dependency.

Three things this module owns that go beyond "just call the API":

1. It generates its OWN Message-ID for every send (a plain uuid4, not
   anything parsed back out of Resend's response) and sets it as an
   explicit outbound header, rather than trusting whatever Resend's API
   response contains. This is what makes reliable threading possible --
   migrations/014_messa_email_messages.sql's thread_id computation depends
   on knowing a message's own Message-ID up front, not after the fact.
   Unverified against a real Resend account from here: some providers
   silently override a custom Message-ID header for their own deliverability
   tracking. Worth one real test after this ships -- send a reply, confirm
   the recipient's own follow-up reply's In-Reply-To actually matches what
   got logged (see db.get_latest_inbound_message_in_thread).
2. It logs every successful send to messa_email_messages itself (via
   db.log_outbound_personal_email), so no future caller of send_email --
   today's two tools, or anything added later -- can forget to log an
   outbound message. This is the one channel module in this project that
   isn't DB-free (channels/sendblue.py stays pure transport, logging
   happens at its call sites instead) -- a deliberate trade of a little
   architectural purity for "an outbound email can never silently vanish
   from the thread history."
3. It's the last line of defense on a `attachment_path` argument before
   anything gets base64-encoded into an actual outbound request: existence,
   "is this actually a file", "is it inside config.OUTPUTS_DIR" (so a
   model-invented path can never make this function read an arbitrary file
   off the server and mail it out), and the size cap
   (config.MAX_EMAIL_ATTACHMENT_BYTES) are all checked here, not just
   trusted from the tool layer that called in -- tools/personal_inbox_tools.py
   also guides Messa toward a real generate_pdf path in its own docstrings/
   system prompt, but this function doesn't rely on that alone.
"""
from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from .. import config, db

BASE_URL = "https://api.resend.com"


class ResendError(RuntimeError):
    """Raised on a non-2xx response from Resend's API, or when Resend isn't
    configured at all."""


def _validate_attachment_path(attachment_path: str) -> Path:
    """Resolves and checks an attachment path before anything gets read off
    disk -- exists, is a plain file, lives inside config.OUTPUTS_DIR (never
    trust a path a model produced to be exactly what it claims), and is
    under the raw-byte size cap. Raises ResendError with a clear, specific
    reason on any failure rather than letting a confusing lower-level
    exception (FileNotFoundError, a PermissionError from escaping the
    output dir, etc.) surface instead."""
    outputs_dir = Path(config.OUTPUTS_DIR).resolve()
    try:
        resolved = Path(attachment_path).resolve()
    except OSError as e:
        raise ResendError(f"Couldn't resolve attachment path {attachment_path!r}: {e}") from e
    if not resolved.is_relative_to(outputs_dir):
        raise ResendError(
            f"Attachment path {attachment_path!r} isn't inside the outputs directory -- "
            "only a file generate_pdf actually produced can be attached."
        )
    if not resolved.is_file():
        raise ResendError(f"Attachment file not found: {attachment_path!r}.")
    size = resolved.stat().st_size
    if size > config.MAX_EMAIL_ATTACHMENT_BYTES:
        limit_mb = config.MAX_EMAIL_ATTACHMENT_BYTES / (1024 * 1024)
        actual_mb = size / (1024 * 1024)
        raise ResendError(
            f"Attachment is too large ({actual_mb:.1f}MB, limit is {limit_mb:.0f}MB) -- "
            "try a shorter document."
        )
    return resolved


async def send_email(
    user_id: int,
    from_local_part: str,
    to: str,
    subject: str,
    text: str,
    *,
    in_reply_to: str | None = None,
    references: str | None = None,
    sent_autonomously: bool = False,
    from_name: str | None = None,
    attachment_path: str | None = None,
) -> dict[str, Any]:
    """Send one email from "<from_local_part>@config.TEXTMESSA_EMAIL_DOMAIN",
    then log it to messa_email_messages under `user_id`.

    `in_reply_to`/`references` are RFC 5322 Message-ID values (e.g.
    "<abc123@example.com>") -- pass them when replying within an existing
    thread so the recipient's own mail client actually threads the reply
    instead of showing it as a new, unrelated message. Resend accepts
    arbitrary outbound headers via its `headers` field, so no special
    "reply" endpoint is needed -- a reply is just a send with these two
    headers set, same as our own Message-ID below.

    `sent_autonomously` is purely for the audit column -- see
    tools/personal_inbox_tools.py's build_personal_inbox_system_prompt for
    the policy that decides what Messa passes here; this function just
    records it, it doesn't interpret it.

    `from_name`: an optional display name (e.g. "Messa, personal assistant
    of Jane") -- sent as `"{from_name} <{from_address}>"` in Resend's
    `from` field so a recipient's mail client shows who's actually writing
    instead of a bare address. None sends just the bare address, unchanged
    from before this parameter existed.

    `attachment_path`: an optional path to a file (currently always a PDF
    from tools/document_tools.py's generate_pdf) to attach -- validated by
    _validate_attachment_path above before anything is read or sent. None
    (the default) sends a plain email with no attachment, unchanged from
    before this parameter existed."""
    if not config.RESEND_API_KEY:
        raise ResendError(
            "Resend isn't configured -- add RESEND_API_KEY to .env to enable Messa's own "
            "email address."
        )

    message_id = f"<{uuid.uuid4()}@{config.TEXTMESSA_EMAIL_DOMAIN}>"
    from_address = f"{from_local_part}@{config.TEXTMESSA_EMAIL_DOMAIN}"
    from_field = f"{from_name} <{from_address}>" if from_name else from_address

    headers: dict[str, str] = {"Message-ID": message_id}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
    if references:
        headers["References"] = references

    payload: dict[str, Any] = {
        "from": from_field,
        "to": [to],
        "subject": subject,
        "text": text,
        "headers": headers,
    }

    attachment_filename: str | None = None
    if attachment_path:
        resolved_path = _validate_attachment_path(attachment_path)
        attachment_filename = resolved_path.name
        encoded = base64.b64encode(resolved_path.read_bytes()).decode("ascii")
        payload["attachments"] = [{"filename": attachment_filename, "content": encoded}]

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{BASE_URL}/emails",
            headers={
                "Authorization": f"Bearer {config.RESEND_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if resp.status_code >= 300:
        raise ResendError(f"Resend send failed ({resp.status_code}): {resp.text[:500]}")
    try:
        response_body = resp.json()
    except Exception:  # noqa: BLE001
        response_body = {}

    # Logged AFTER a confirmed 2xx -- only actually-sent mail enters the
    # thread history, never an attempt that failed partway (the caller sees
    # the ResendError above and never reaches here).
    await db.log_outbound_personal_email(
        user_id=user_id,
        message_id=message_id,
        from_address=from_address,
        to_address=to,
        subject=subject,
        body_text=text,
        in_reply_to=in_reply_to,
        references=references,
        raw_json=json.dumps({"request": payload, "response": response_body}),
        sent_autonomously=sent_autonomously,
        attachment_filename=attachment_filename,
    )

    return response_body
