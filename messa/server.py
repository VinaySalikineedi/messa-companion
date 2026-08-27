"""Phase 2: the always-on webhook server that lets you text Messa over
Sendblue (iMessage/SMS/RCS).

Run: uvicorn messa.server:app --host 0.0.0.0 --port 8000

One inbound webhook, three steps:
  1. Verify + filter (drop echoes of our own outbound messages, and any
     event type other than an inbound message -- point `sendblue webhooks
     set-receive` only at this URL and this shouldn't come up, but it's
     cheap insurance).
  2. ACK Sendblue with 200 immediately. Sendblue waits ~45s for a response
     and retries (up to 3x) on timeout/failure -- a full Messa turn
     (especially one that delegates to deepsearch) can easily take longer
     than that, so the actual work happens in a BackgroundTask *after* the
     response is sent, not before it.
  3. Do the work: resolve/create the user by phone number, run one turn
     through Messa (reusing cli.py's run_message -- see its docstring for
     why the webhook path loads history from the DB instead of keeping an
     in-memory list like the CLI's REPL does), and POST the reply back via
     the Sendblue send-message API. A typing indicator goes out first as a
     best-effort UX nicety (silently ignored if it fails -- see round 1's
     "slowness between steps" feedback; the CLI got a "checking flights
     now..." acknowledgment for the same reason, this is the SMS/iMessage
     equivalent).

Approval: destructive deepsearch/email actions can't block on stdin here
(no synchronous human on the other end of an HTTP request) -- see
approval.DenyApprovalGate and MESSA_SMS_AUTO_APPROVE_DESTRUCTIVE in
config.py for the tradeoff and how to opt out of the safe default.
"""
from __future__ import annotations

from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.responses import JSONResponse

from . import cli, config, console, db
from .agents.registry import build_orchestrator
from .approval import AutoApproveGate, DenyApprovalGate
from .channels import sendblue
from .channels.sendblue import SendblueError

app = FastAPI(title="Messa Sendblue webhook")

# iMessage/SMS both map to the same subagent behavior today; the distinction
# is only surfaced to Messa's system prompt as the channel string in case a
# future prompt tweak wants to tell them apart (e.g. media support differs).
_CHANNEL_BY_SERVICE = {"imessage": "imessage", "sms": "sms", "rcs": "rcs"}


def _approval_gate():
    return AutoApproveGate() if config.SMS_AUTO_APPROVE_DESTRUCTIVE else DenyApprovalGate()


@app.get("/")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "messa-sendblue-webhook"}


@app.post("/webhook/sendblue")
@app.post("/sms/sendblue/webhook")
async def sendblue_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    sb_signing_secret: str | None = Header(default=None, alias="sb-signing-secret"),
) -> JSONResponse:
    if config.SENDBLUE_WEBHOOK_SECRET and sb_signing_secret != config.SENDBLUE_WEBHOOK_SECRET:
        console.system("Sendblue webhook: rejected request with bad/missing sb-signing-secret.")
        return JSONResponse({"error": "invalid signing secret"}, status_code=401)

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    # Defensive filtering: only act on genuine inbound messages with content.
    # (Outbound-echo/typing_indicator/etc. webhooks shouldn't reach this URL
    # if it's only registered for `receive`, but a shared endpoint or a
    # dashboard misconfiguration could still send one.)
    if payload.get("is_outbound"):
        return JSONResponse({"status": "ignored (outbound echo)"})

    from_number = payload.get("from_number")
    content = (payload.get("content") or "").strip()
    if not from_number or not content:
        return JSONResponse({"status": "ignored (no from_number/content)"})

    service = (payload.get("service") or "sms").strip().lower()
    channel = _CHANNEL_BY_SERVICE.get(service, "sms")

    background_tasks.add_task(_process_inbound, from_number, content, channel)
    return JSONResponse({"status": "accepted"})


async def _process_inbound(from_number: str, content: str, channel: str) -> None:
    try:
        await sendblue.send_typing_indicator(from_number)
    except SendblueError as e:
        console.system(f"Sendblue: typing indicator failed (non-fatal): {e}")

    try:
        user = await cli.load_user_context(from_number, name=None, channel=channel)
        agent = await build_orchestrator(user, _approval_gate())
        reply = await cli.run_message(user, agent, content)
    except Exception as e:  # noqa: BLE001 - a webhook background task must never raise unseen
        console.system(f"Sendblue: turn failed for {from_number}: {e}")
        reply = (
            "Sorry, something went wrong on my end handling that -- mind trying again "
            "in a moment?"
        )

    if not reply:
        return

    try:
        await sendblue.send_message(from_number, reply)
    except SendblueError as e:
        console.system(f"Sendblue: failed to deliver reply to {from_number}: {e}")
        return

    try:
        await sendblue.mark_read(from_number)
    except SendblueError:
        pass  # cosmetic only


@app.on_event("shutdown")
async def _shutdown() -> None:
    await db.close_pool()
