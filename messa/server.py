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

import asyncio
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import background, cli, config, console, db, live_activity
from .agents.registry import build_orchestrator
from .approval import AutoApproveGate, DenyApprovalGate
from .channels import sendblue
from .channels.sendblue import SendblueError
from .live_view_page import render_live_view_page
from .tools.routines_tools import compute_next_run

app = FastAPI(title="Messa Sendblue webhook")

# Populated by `_startup` below, cancelled by `_shutdown`.
_bg_tasks: list[asyncio.Task] = []

# iMessage/SMS both map to the same subagent behavior today; the distinction
# is only surfaced to Messa's system prompt as the channel string in case a
# future prompt tweak wants to tell them apart (e.g. media support differs).
_CHANNEL_BY_SERVICE = {"imessage": "imessage", "sms": "sms", "rcs": "rcs"}


def _approval_gate():
    return AutoApproveGate() if config.SMS_AUTO_APPROVE_DESTRUCTIVE else DenyApprovalGate()


@app.get("/")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "messa-sendblue-webhook"}


@app.get("/live/{token}")
async def live_view_page(token: str, style: str | None = None) -> HTMLResponse:
    """Phase 3: the public page Messa's live-view link points at (see
    agents/registry.py). Always returns 200 with the page shell regardless
    of whether the token is valid -- the page's own JS calls the status
    route below to find out, and renders an "invalid link" state itself on
    a 404 from that. Keeping this route token-agnostic (no DB lookup here)
    means a bad/typo'd link still loads something sensible instantly
    instead of a bare framework 404.

    `style`: optional `?style=polished` or `?style=terminal` query param --
    overrides live_view_page.LIVE_VIEW_STYLE for just this one page load,
    so you can compare both skins by editing the URL rather than redeploying
    (see that module's docstring)."""
    return HTMLResponse(render_live_view_page(token, style))


@app.get("/live/{token}/status")
async def live_view_status(token: str) -> JSONResponse:
    """Polled by the page above every few seconds. 404 means "no such
    link" (the page shows 'this link isn't valid' and stops polling); 200
    with active=false means "valid link, nothing running right now".

    When active, also attaches `description` (what deepsearch is doing
    right now, one line), `steps` (this task's chain-of-thought log so far),
    and `closing` (true for the brief window between "the run finished" and
    "the Browserbase session is actually released" -- see
    tools/deepsearch_tools.py's `set_closing` call -- so the page can swap
    to a clean "Compiling your results..." screen instead of showing
    Browserbase's own CDP-disconnect banner) from live_activity.py's
    in-memory per-user log. All three go back to None/[]/false the instant
    `active` is false, regardless of whatever live_activity still happens to
    hold, so a stale in-memory log can never outlive what the DB says is
    actually running."""
    status = await db.get_live_status_by_token(token)
    if status is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    user_id = status.pop("user_id", None)
    activity = live_activity.get(user_id) if (status["active"] and user_id is not None) else None
    status["description"] = activity["description"] if activity else None
    status["steps"] = activity["steps"] if activity else []
    status["closing"] = activity["closing"] if activity else False
    return JSONResponse(status)


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

    async def _send(text: str) -> None:
        """Passed into cli.run_message as its `send` callback -- called
        immediately for every AI message Messa produces this turn, not
        just the last one. This is what actually fixes the missing
        live-view link: Messa's pre-delegation acknowledgment ("Checking
        that now, watch it live here: <link>") used to only ever be logged
        server-side, since the old code waited for the whole turn
        (including a possibly multi-minute deepsearch run) to finish and
        texted only its final message. Now each of her utterances goes out
        as its own SMS the moment she says it -- matching how a person
        actually texts, and meaning the live link arrives *before* the
        browsing starts, when it's actually useful."""
        try:
            await sendblue.send_message(from_number, text)
        except SendblueError as e:
            console.system(f"Sendblue: failed to deliver a message to {from_number}: {e}")
            return
        # Re-arm the typing indicator after each text -- Sendblue's own
        # indicator doesn't persist across a real outbound message, and if
        # more work follows (e.g. Messa just delegated to deepsearch), the
        # user should see "typing..." again while that runs rather than
        # nothing until the next text arrives.
        try:
            await sendblue.send_typing_indicator(from_number)
        except SendblueError:
            pass

    try:
        user = await cli.load_user_context(from_number, name=None, channel=channel)
        agent = await build_orchestrator(user, _approval_gate())
        await cli.run_message(user, agent, content, send=_send)
    except Exception as e:  # noqa: BLE001 - a webhook background task must never raise unseen
        console.system(f"Sendblue: turn failed for {from_number}: {e}")
        await _send(
            "Sorry, something went wrong on my end handling that -- mind trying again "
            "in a moment?"
        )

    try:
        await sendblue.mark_read(from_number)
    except SendblueError:
        pass  # cosmetic only


async def _production_reminder_loop() -> None:
    """The real counterpart to background.py's CLI-only preview poller
    (which only ever printed to a local terminal, and was never wired into
    this server at all -- so due reminders were being scheduled correctly
    in the DB but never actually reaching a real user's phone in
    production). This one actually delivers via Sendblue, and only marks a
    reminder sent once delivery succeeds -- a Sendblue failure leaves it
    pending so the next poll retries it instead of silently losing it."""
    while True:
        try:
            due = await db.get_due_reminders_for_delivery()
            for r in due:
                try:
                    await sendblue.send_message(r["phone_number"], f"Reminder: {r['message']}")
                except SendblueError as e:
                    console.system(f"[reminder delivery failed] user={r['user_id']} reminder=#{r['id']}: {e}")
                    continue
                await db.mark_reminder_sent(r["id"])
        except Exception as e:  # noqa: BLE001 - a background loop must never die silently mid-process
            console.system(f"[reminder poller error] {e}")
        await asyncio.sleep(background.POLL_INTERVAL_SECONDS)


async def _production_cron_loop() -> None:
    """The real counterpart to background.py's CLI-only cron preview (which
    only ever printed "would run now"). This re-invokes Messa for real with
    the job's saved prompt_or_task, using the same load_user_context/
    build_orchestrator/run_message path a live inbound text would use, and
    delivers whatever Messa produces via Sendblue -- so a recurring
    automation (a daily briefing, a weekly check-in) actually happens
    instead of only ever being provably schedulable."""
    while True:
        try:
            due = await db.get_due_cron_jobs_for_delivery()
            for job in due:
                phone = job["phone_number"]
                try:
                    user = await cli.load_user_context(phone, channel="sms")
                    agent = await build_orchestrator(user, _approval_gate())

                    async def _cron_send(text: str, _phone: str = phone) -> None:
                        await sendblue.send_message(_phone, text)

                    await cli.run_message(user, agent, job["prompt_or_task"], send=_cron_send)
                except Exception as e:  # noqa: BLE001
                    console.system(f"[cron delivery failed] job=#{job['id']}: {e}")
                next_run = compute_next_run(job["cron_expression"], job["user_timezone"])
                await db.reschedule_cron_job(job["id"], next_run)
        except Exception as e:  # noqa: BLE001
            console.system(f"[cron poller error] {e}")
        await asyncio.sleep(background.POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    global _bg_tasks
    _bg_tasks = [
        asyncio.create_task(_production_reminder_loop()),
        asyncio.create_task(_production_cron_loop()),
    ]
    console.system("Started production reminder/cron delivery pollers.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    for t in _bg_tasks:
        t.cancel()
    await db.close_pool()
