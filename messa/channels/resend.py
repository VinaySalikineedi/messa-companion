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

Two things this module owns that go beyond "just call the API":

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
"""
from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from .. import config, db

BASE_URL = "https://api.resend.com"


class ResendError(RuntimeError):
    """Raised on a non-2xx response from Resend's API, or when Resend isn't
    configured at all."""


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
    tools/personal_inbox_tools.py's PERSONAL_INBOX_SYSTEM_PROMPT for the
    policy that decides what Messa passes here; this function just records
    it, it doesn't interpret it."""
    if not config.RESEND_API_KEY:
        raise ResendError(
            "Resend isn't configured -- add RESEND_API_KEY to .env to enable Messa's own "
            "email address."
        )

    message_id = f"<{uuid.uuid4()}@{config.TEXTMESSA_EMAIL_DOMAIN}>"
    from_address = f"{from_local_part}@{config.TEXTMESSA_EMAIL_DOMAIN}"

    headers: dict[str, str] = {"Message-ID": message_id}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
    if references:
        headers["References"] = references

    payload: dict[str, Any] = {
        "from": from_address,
        "to": [to],
        "subject": subject,
        "text": text,
        "headers": headers,
    }

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
    )

    return response_body
