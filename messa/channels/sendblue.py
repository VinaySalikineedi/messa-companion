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


# ---------------------------------------------------------------------------
# Contact Sharing (Name & Photo) -- a SEPARATE Sendblue API surface from
# everything above: it lives on api.sendblue.com/api/v2/ (note the .com
# domain and /v2/ prefix, NOT the api.sendblue.co/api/<endpoint> shape the
# messaging endpoints above use -- confirmed against Sendblue's own docs at
# https://docs.sendblue.com/api-v2/contact-sharing/, not a typo). Lets this
# number publish a business name + photo that shows on the recipient's side
# of an iMessage thread (their Contacts app / message header), the way a
# verified business number does -- separate from anything sent as an actual
# message. Untested end to end as of this writing: this sandbox's own
# outbound network access doesn't reach api.sendblue.com (proxy-blocked),
# so these have only been checked against the documented request/response
# shapes, not fired for real -- see scripts/publish_contact_profile.py's own
# docstring for the real, human-run verification step.
_CONTACT_SHARING_BASE_URL = "https://api.sendblue.com/api/v2"


async def _contact_sharing_request(
    method: str, endpoint: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None
) -> dict[str, Any]:
    _require_credentials()
    headers = {
        "sb-api-key-id": config.SENDBLUE_API_KEY,
        "sb-api-secret-key": config.SENDBLUE_API_SECRET,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method, f"{_CONTACT_SHARING_BASE_URL}/{endpoint}", headers=headers, params=params, json=json_body,
        )
    if resp.status_code >= 300:
        raise SendblueError(f"Sendblue contact-sharing {endpoint} failed ({resp.status_code}): {resp.text[:500]}")
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


async def set_contact_profile(
    first_name: str | None = None,
    last_name: str | None = None,
    photo_url: str | None = None,
    clear_photo: bool = False,
    from_number: str | None = None,
) -> dict[str, Any]:
    """Create or update this number's shared business name/photo.
    `photo_url` must be a publicly-reachable direct JPEG or PNG URL (per
    Sendblue's docs -- no base64 upload). Pass empty string for
    first_name/last_name to clear just that field; pass clear_photo=True to
    remove the photo specifically (mutually exclusive with photo_url --
    raises ValueError if both are given, since Sendblue's own docs say they
    can't be combined in one call). `from_number` defaults to this
    project's own config.SENDBLUE_NUMBER -- only pass it explicitly if this
    project ever manages more than one Sendblue line.

    NOTE: setting the profile does NOT push it into any EXISTING iMessage
    thread by itself -- see share_contact_profile below for that, and
    Sendblue's own docs: 'Applying the profile may continue after the
    response, so do not retry only because the change is not visible
    immediately.'"""
    if photo_url and clear_photo:
        raise ValueError("set_contact_profile: pass photo_url OR clear_photo=True, never both.")
    payload: dict[str, Any] = {"fromNumber": from_number or config.SENDBLUE_NUMBER}
    if first_name is not None:
        payload["firstName"] = first_name
    if last_name is not None:
        payload["lastName"] = last_name
    if photo_url:
        payload["photoUrl"] = photo_url
    if clear_photo:
        payload["clearPhoto"] = True
    return await _contact_sharing_request("POST", "contact-sharing/profile", json_body=payload)


async def get_contact_profile_state(from_number: str | None = None) -> dict[str, Any]:
    """Read-only: this number's current profile config + whether sharing is
    enabled (e.g. `{"hasProfile": true, "sharingEnabled": true, "firstName":
    ..., "displayName": ..., "hasPhoto": true}` per Sendblue's docs) -- use
    this to check "is it already set" before deciding whether to call
    set_contact_profile at all."""
    return await _contact_sharing_request(
        "GET", "contact-sharing/state", params={"fromNumber": from_number or config.SENDBLUE_NUMBER},
    )


async def share_contact_profile(to_number: str, from_number: str | None = None) -> dict[str, Any]:
    """Explicitly push the already-configured profile (set via
    set_contact_profile) into ONE existing direct iMessage conversation --
    per Sendblue's docs this does NOT happen automatically just from
    calling set_contact_profile, and does not itself send a text message.
    Sendblue dedupes repeat calls for the same (from_number, to_number)
    pair within 24h (returns `deduplicated: true` rather than erroring), so
    calling this again for someone already shared-with recently is safe/
    cheap, not a real re-send. A successful response only confirms the
    share REQUEST went out -- not that the recipient's device has actually
    shown it yet."""
    return await _contact_sharing_request(
        "POST", "contact-sharing/share",
        json_body={"fromNumber": from_number or config.SENDBLUE_NUMBER, "toNumber": to_number},
    )


async def delete_contact_profile(from_number: str | None = None) -> dict[str, Any]:
    """Disables sharing and removes the profile entirely -- use this to
    fully undo set_contact_profile, not clear_photo=True (which only clears
    the photo, keeping the name/sharing state)."""
    return await _contact_sharing_request(
        "DELETE", "contact-sharing/profile", json_body={"fromNumber": from_number or config.SENDBLUE_NUMBER},
    )

