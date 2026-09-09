"""Thin async client for the handful of Vapi AI endpoints the outbound
voice-calling feature uses (plans/glowing-forging-pumpkin.md).

Same shape as messa/channels/sendblue.py on purpose: module-level BASE_URL,
one typed error class, a _require_credentials() that names exactly which
env vars are missing, one request helper, thin typed async functions per
endpoint. Nothing outside this module (and messa/config.py, for the env
vars themselves) should ever read config.VAPI_* directly or build a Vapi
URL/header by hand -- that's what keeps "swap providers later" (the
product's own explicit "fully pluggable" requirement) a one-file change.

Vapi chosen over Retell AI specifically for two documented, API-accessible
capabilities Retell's dashboard-only equivalent doesn't offer today: a
listenUrl WebSocket for real-time call audio (see messa/call_activity.py /
server.py's live-listen relay) and dynamic per-call outbound assistant
configuration passed directly in the create-call request body.

No Vapi account exists yet as of this writing -- every function below is
built and tested against Vapi's PUBLISHED API docs, not verified live.
The plan itself flags a short list of things to re-check against Vapi's
CURRENT docs the moment a real account/key exists, before ever placing a
real call (see the module-level FLAG_FOR_GO_LIVE_VERIFICATION comment
below) -- do not trust any exact JSON shape here as gospel once real
credentials exist.
"""
from __future__ import annotations

from typing import Any

import httpx

from .. import config

BASE_URL = "https://api.vapi.ai"

# Flagged explicitly (plan Section 3) for verification against Vapi's
# CURRENT docs at go-live time, before the first real call:
#   (a) the exact end-of-call-report webhook shape / where billed duration
#       actually lives in it (assumed here: message.durationSeconds or
#       similar on a "end-of-call-report" message type);
#   (b) Vapi's actual webhook authenticity mechanism (assumed here: a
#       static shared-secret header, mirroring Sendblue's sb-signing-
#       secret already used in this repo -- NOT an HMAC scheme, unless
#       Vapi's docs say otherwise);
#   (c) the exact "hang up an in-progress call" API shape (assumed here:
#       PATCH /call/{id} with an ended/end status, per Vapi's REST docs
#       for updating a call);
#   (d) the exact mid-call tool-call webhook message/response envelope;
#   (e) whether relaying listenUrl server-side needs any auth beyond
#       possessing the URL;
#   (f) the exact transient-assistant JSON shape for maxDurationSeconds /
#       model.tools on an outbound /call request.
FLAG_FOR_GO_LIVE_VERIFICATION = True


class VapiError(RuntimeError):
    """Raised on a non-2xx response from Vapi's API, or a missing-credentials call."""


def _require_credentials() -> None:
    missing = [
        name
        for name, val in (
            ("VAPI_API_KEY", config.VAPI_API_KEY),
            ("VAPI_PHONE_NUMBER_ID", config.VAPI_PHONE_NUMBER_ID),
        )
        if not val
    ]
    if missing:
        raise VapiError(
            f"Vapi not configured -- missing {', '.join(missing)} in .env. "
            "Voice calling isn't set up on this account yet."
        )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {config.VAPI_API_KEY}",
        "Content-Type": "application/json",
    }


async def _post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    _require_credentials()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{BASE_URL}/{endpoint}", headers=_headers(), json=payload)
    if resp.status_code >= 300:
        raise VapiError(f"Vapi {endpoint} failed ({resp.status_code}): {resp.text[:500]}")
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


async def _get(endpoint: str) -> dict[str, Any]:
    _require_credentials()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(f"{BASE_URL}/{endpoint}", headers=_headers())
    if resp.status_code >= 300:
        raise VapiError(f"Vapi {endpoint} failed ({resp.status_code}): {resp.text[:500]}")
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


async def _patch(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    _require_credentials()
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.patch(f"{BASE_URL}/{endpoint}", headers=_headers(), json=payload)
    if resp.status_code >= 300:
        raise VapiError(f"Vapi {endpoint} failed ({resp.status_code}): {resp.text[:500]}")
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return {}


async def create_call(
    destination_number: str,
    *,
    assistant: dict[str, Any],
    metadata: dict[str, Any],
    max_duration_seconds: int,
) -> dict[str, Any]:
    """Places one outbound call. Messa never receives INBOUND calls in this
    design, so -- unlike Vapi's inbound "assistant-request" webhook flow
    (7.5s response budget to hand back an assistant config for a call
    Vapi didn't originate) -- the assistant's full configuration is simply
    included directly in THIS one request. `assistant` is the transient,
    per-call config built by call_tools.dial_confirmed_call (system
    prompt with the untrusted-input framing, the scoped mid-call info
    tool, maxDurationSeconds); `metadata` must include
    {"call_session_id": ...} for webhook correlation back to our own
    call_sessions row (see db.get_call_session_by_provider_id).

    `max_duration_seconds` is enforced by Vapi itself via the assistant's
    own maxDurationSeconds field -- the caller (call_tools.py) is
    responsible for computing it as min(config.CALL_MAX_DURATION_SECONDS,
    remaining_monthly_minutes * 60) BEFORE calling this; this function
    does not re-derive or re-check that number, it only forwards it."""
    assistant_config = dict(assistant)
    assistant_config["maxDurationSeconds"] = max_duration_seconds
    payload = {
        "phoneNumberId": config.VAPI_PHONE_NUMBER_ID,
        "customer": {"number": destination_number},
        "assistant": assistant_config,
        "metadata": metadata,
    }
    return await _post("call", payload)


async def get_call(provider_call_id: str) -> dict[str, Any]:
    """Read-only status/details fetch by Vapi's own call id."""
    return await _get(f"call/{provider_call_id}")


async def end_call(provider_call_id: str) -> dict[str, Any]:
    """Hangs up an in-progress call. See FLAG_FOR_GO_LIVE_VERIFICATION(c)
    above -- the exact shape (PATCH vs a dedicated endpoint) must be
    reconfirmed against Vapi's current docs before this is ever relied on
    for a real call; the request shape here is a best-effort placeholder
    built from their documented call-update pattern."""
    return await _patch(f"call/{provider_call_id}", {"status": "ended"})


def verify_webhook_request(headers: dict[str, str], body: bytes) -> bool:
    """True iff this inbound webhook request actually came from Vapi.
    See FLAG_FOR_GO_LIVE_VERIFICATION(b) above: assumed here to be a
    static shared-secret header (mirroring Sendblue's own sb-signing-
    secret pattern already used in this codebase), NOT an HMAC scheme --
    reconfirm against Vapi's current docs before go-live and adjust this
    function's body if their actual mechanism differs.

    If config.VAPI_WEBHOOK_SECRET is unset, this returns True unverified
    (mirrors SENDBLUE_WEBHOOK_SECRET's own "fine for local dev, set it
    once the URL is public" posture) -- but see that config var's own
    comment: a real deployment with VAPI_API_KEY set should treat an
    unset VAPI_WEBHOOK_SECRET as a configuration error for this
    specific, higher-stakes surface, not silently accept every request."""
    if not config.VAPI_WEBHOOK_SECRET:
        return True
    provided = headers.get("x-vapi-secret") or headers.get("X-Vapi-Secret")
    return provided is not None and provided == config.VAPI_WEBHOOK_SECRET
