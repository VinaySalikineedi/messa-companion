"""Thin async client for the handful of Sendblue endpoints this project uses.

Base URL and auth headers per the user's own Sendblue quickstart doc:
POST https://api.sendblue.co/api/<endpoint>, headers `sb-api-key-id` /
`sb-api-secret-key`. No SDK dependency -- three endpoints doesn't warrant
one, and httpx is already a transitive dependency of several packages here.

Sendblue handles iMessage/SMS/RCS fallback and message segmentation itself
(content up to ~19k chars per their docs), so replies don't need manual SMS
160-char splitting the way a raw Twilio SMS integration would.
"""
from __future__ import annotations

from typing import Any

import httpx

from .. import config

BASE_URL = "https://api.sendblue.co/api"


class SendblueError(RuntimeError):
    """Raised on a non-2xx response from Sendblue's API."""


def _require_credentials() -> None:
    missing = [
        name
        for name, val in (
            ("SENDBLUE_API_KEY", config.SENDBLUE_API_KEY),
            ("SENDBLUE_API_SECRET", config.SENDBLUE_API_SECRET),
            ("SENDBLUE_NUMBER", config.SENDBLUE_NUMBER),
        )
        if not val
    ]
    if missing:
        raise SendblueError(
            f"Sendblue not configured -- missing {', '.join(missing)} in .env. "
            "Run `sendblue show-keys` / `sendblue lines` to get these."
        )


async def _post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    _require_credentials()
    headers = {
        "sb-api-key-id": config.SENDBLUE_API_KEY,
        "sb-api-secret-key": config.SENDBLUE_API_SECRET,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{BASE_URL}/{endpoint}", headers=headers, json=payload)
    if resp.status_code >= 300:
        raise SendblueError(f"Sendblue {endpoint} failed ({resp.status_code}): {resp.text[:500]}")
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


async def send_message(
    number: str,
    content: str,
    *,
    media_url: str | None = None,
    send_style: str | None = None,
) -> dict[str, Any]:
    """Send a reply. `number` is the recipient in E.164 format.
    `send_style` applies an Apple iMessage effect (e.g. 'confetti', 'celebration',
    'fireworks', 'lasers', 'love', 'balloons', 'spotlight', 'echo', 'invisible',
    'gentle', 'loud', 'slam'). Best-effort -- Sendblue falls back gracefully on SMS."""
    payload: dict[str, Any] = {
        "number": number,
        "from_number": config.SENDBLUE_NUMBER,
        "content": content,
    }
    if media_url:
        payload["media_url"] = media_url
    if send_style:
        payload["send_style"] = send_style.strip().lower()
    return await _post("send-message", payload)


async def send_reaction(
    number: str,
    message_handle: str,
    reaction: str,
) -> dict[str, Any]:
    """Send a tapback reaction to an incoming message (iMessage only).
    `reaction` can be: 'love', 'like', 'dislike', 'laugh', 'emphasize', 'question',
    or a single emoji (e.g. '👍', '❤️', '🔥')."""
    return await _post(
        "send-reaction",
        {
            "from_number": config.SENDBLUE_NUMBER,
            "number": number,
            "message_handle": message_handle,
            "reaction": reaction.strip(),
        },
    )


async def send_typing_indicator(number: str) -> dict[str, Any]:
    """Show the "..." bubble (iMessage only -- Sendblue no-ops harmlessly for
    SMS recipients). Best-effort: callers should swallow SendblueError here,
    it's a UX nicety while Messa/subagents are working, not required."""
    return await _post("send-typing-indicator", {"number": number, "from_number": config.SENDBLUE_NUMBER})


async def mark_read(number: str) -> dict[str, Any]:
    """Send a read receipt for the inbound message just processed."""
    return await _post("mark-read", {"number": number, "from_number": config.SENDBLUE_NUMBER})

