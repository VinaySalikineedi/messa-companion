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
"""
from __future__ import annotations

from typing import Any

import httpx

from .. import config

BASE_URL = "https://api.resend.com"


class ResendError(RuntimeError):
    """Raised on a non-2xx response from Resend's API, or when Resend isn't
    configured at all."""


async def send_email(
    from_local_part: str,
    to: str,
    subject: str,
    text: str,
    *,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> dict[str, Any]:
    """Send one email from "<from_local_part>@config.TEXTMESSA_EMAIL_DOMAIN".

    `in_reply_to`/`references` are RFC 5322 Message-ID values (e.g.
    "<abc123@example.com>") -- pass them when replying within an existing
    thread so the recipient's own mail client actually threads the reply
    instead of showing it as a new, unrelated message. Resend accepts
    arbitrary outbound headers via its `headers` field, so no special
    "reply" endpoint is needed -- a reply is just a send with these two
    headers set."""
    if not config.RESEND_API_KEY:
        raise ResendError(
            "Resend isn't configured -- add RESEND_API_KEY to .env to enable Messa's own "
            "email address."
        )

    headers: dict[str, str] = {}
    if in_reply_to:
        headers["In-Reply-To"] = in_reply_to
    if references:
        headers["References"] = references

    payload: dict[str, Any] = {
        "from": f"{from_local_part}@{config.TEXTMESSA_EMAIL_DOMAIN}",
        "to": [to],
        "subject": subject,
        "text": text,
    }
    if headers:
        payload["headers"] = headers

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
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}
