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
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import BackgroundTasks, FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import background, cli, config, console, db, live_activity
from .agents.registry import build_orchestrator
from .approval import AutoApproveGate, DenyApprovalGate
from .channels import browserbase, sendblue
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

    When active, also attaches `closing` (true for the brief window between
    "the run finished" and "the Browserbase session is actually released" --
    see tools/deepsearch_tools.py's `set_closing` call -- so the page can
    swap to a clean "Compiling your results..." screen instead of showing
    Browserbase's own CDP-disconnect banner) and `tiles` -- one entry per
    currently-open browser tab (the top-level orchestrator tab plus every
    live `delegate_website_task` sub-worker), each with its OWN `heading`,
    `description`, `steps`, `waiting_for_human`, and `live_view_url` -- so
    the page can render every tab someone is actively working, not just one
    fixed view (see _build_live_tiles below for how each tab's own
    live_view_url is resolved). `tiles` is `[]` (not omitted) whenever
    `closing` is true -- the page shows one unified "wrapping up" screen at
    that point rather than a grid mid-teardown. Everything goes back to
    empty/false the instant `active` is false, regardless of whatever
    live_activity still happens to hold, so a stale in-memory log can never
    outlive what the DB says is actually running."""
    status = await db.get_live_status_by_token(token)
    if status is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    user_id = status.pop("user_id", None)
    activity = live_activity.get(user_id) if (status["active"] and user_id is not None) else None
    closing = activity["closing"] if activity else False
    tiles = await _build_live_tiles(status, activity) if (activity and not closing) else []
    return JSONResponse({"active": status["active"], "closing": closing, "tiles": tiles})


def _tile_heading(url: str | None) -> str:
    """A short, human-friendly label for one tab's tile heading -- the
    hostname of whatever it's working on (e.g. "booking.com"), matching how
    a browser's own tab strip labels tabs. Falls back for anything that
    isn't a normal http(s) url (a bare data: url mid-navigation, or no url
    yet at all right as a tab opens)."""
    if not url:
        return "New tab"
    try:
        host = urlparse(url).netloc
    except Exception:  # noqa: BLE001
        host = ""
    if not host:
        return "New tab"
    return host[4:] if host.startswith("www.") else host


_GENERIC_PAGE_TITLES = {"", "about:blank", "new tab", "untitled"}


def _real_page_title(page: dict) -> str | None:
    """The actual <title> Chrome reports for this specific page, as
    Browserbase's pages[] surfaces it (channels/browserbase.get_session_pages)
    -- e.g. "Google Flights" and "Google Hotels" are two DIFFERENT titles
    even though both tabs sit on the same google.com host, which is exactly
    the case a hostname-only heading (_tile_heading) can't distinguish, and
    exactly what was previously shown as a generic label ("Search live
    flight and hotel options for a...", the top-level's own task
    description, per a host-key collision -- see _build_live_tiles). Prefer
    this over hostname whenever it looks like a genuine title; None for a
    still-loading/blank page so the caller falls through to hostname."""
    title = (page.get("title") or "").strip()
    if not title or title.lower() in _GENERIC_PAGE_TITLES:
        return None
    return title


def _host_key(url: str | None) -> str | None:
    """Host+path matching key for _build_live_tiles's enrichment step -- a
    real, reproducible fix for "the tiles stopped showing live
    thoughts/actions" once tile existence started coming straight from
    Browserbase's own pages[] (see that function's own docstring): matching
    a real page to our own tracked description/steps by EXACT url equality
    is fragile in practice -- what we last told @playwright/mcp to navigate
    to (`activity["url"]`/a sub-worker's `tabs[id]["url"]`) and what
    Browserbase's pages[] later reports as that tab's real, current url
    routinely differ on a redirect, an added/stripped trailing slash, a
    www. prefix, or a query string the site itself appends -- any of which
    would silently make `known_by_url.get(url)` miss, leaving that tile's
    description/log permanently empty even while real work is happening on
    it.

    Originally this was bare hostname (DEEPSEARCH_SYSTEM_PROMPT enforces one
    website per delegate_website_task call, so ordinarily every concurrently
    open tab sits on a distinct host). That broke down for two tabs on the
    SAME host with different jobs -- concretely, a Google Flights tab and a
    Google Hotels tab both live on google.com, and bare-host matching
    collapsed them onto the same tracked entry, which is what made both
    tiles show the SAME (wrong) heading in a real run. Including the path
    (query string still excluded, since sites routinely append/reorder their
    own params on the exact same logical page -- see the expedia.com test
    case) keeps the redirect/www/trailing-slash tolerance this was built for
    while telling .../travel/flights apart from .../travel/hotels. Returns
    None for anything with no real, distinguishing host (a still-blank tab,
    a data: url mid-navigation) -- matching on that would risk attaching one
    blank tab's log to a different one."""
    heading = _tile_heading(url)
    if heading == "New tab":
        return None
    try:
        path = urlparse(url).path.rstrip("/").lower()
    except Exception:  # noqa: BLE001
        path = ""
    return f"{heading}{path}"


def _hide_navbar(url: str | None) -> str | None:
    """Appends Browserbase's documented `navbar=false` param to a live-view
    url, hiding its own embedded chrome (address bar/tab strip) so the tile
    shows just the page -- see https://docs.browserbase.com/platform/
    browser/observability/session-live-view ("styling" section). None-safe
    (a still-resolving tile's url is legitimately None)."""
    if not url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}navbar=false"


async def _build_live_tiles(status: dict, activity: dict) -> list[dict]:
    """Builds one tile per REAL open browser tab, straight from
    Browserbase's own pages[] (channels/browserbase.get_session_pages) --
    NOT from our own delegate_website_task bookkeeping (live_activity's
    `tabs` dict), which undercounted for real in an earlier round: a real
    test run had 4 tabs genuinely open in the one Browserbase session but
    only 1 tile ever rendered, because a tab can exist without ever going
    through delegate_website_task (the top-level connection's own model can
    open extra tabs itself -- a link with target="_blank", or calling
    browser_tabs directly; it's on the top-level's toolset like every other
    @playwright/mcp tool). Tying tile EXISTENCE to Browserbase's own
    pages[] instead fixes this categorically: whatever tabs are really
    open, for whatever reason, get a tile.

    live_activity's own tracked state (top-level `url`/description/steps,
    and each delegate_website_task sub-worker's own entry in `tabs`) is
    used ONLY as best-effort enrichment -- matched against a real page by
    url, to fill in a nicer heading (the task title for whichever page is
    the top-level's current one) and the running description/step log we
    already have for it. A real page with no match still gets a perfectly
    good tile, just with a hostname-derived heading and an empty log.

    Resilience fallback (added after a real run showed a completely BLANK
    page -- no tiles at all -- while tabs were genuinely open and running):
    whenever `get_session_pages` has nothing usable (no bb_session_id yet,
    the call itself fails, or it comes back empty -- Browserbase's pages[]
    isn't push-live, so a fresh session can have a real gap before its
    first page is indexed), this falls back to ONE tile built from the
    session-level info the DB already has (`status["live_view_url"]`,
    resolved once at session-open time via browserbase.get_live_view_url
    and proven reliable since Phase 3's very first version) rather than
    returning no tiles at all. The page should never go blank while
    `active` is true -- worst case, it shows one tile instead of several
    until per-page data becomes available again."""
    bb_session_id = activity.get("bb_session_id")
    pages: list[dict] = []
    if bb_session_id:
        try:
            pages = await browserbase.get_session_pages(bb_session_id)
        except Exception as e:  # noqa: BLE001
            console.tool_error("deepsearch", "browserbase_session_pages", str(e))
            pages = []

    if not pages:
        if not status.get("live_view_url"):
            return []
        return [{
            "id": "top",
            "heading": status.get("task") or "Deepsearch",
            "description": activity.get("description"),
            "steps": activity.get("steps") or [],
            "waiting_for_human": activity.get("waiting_for_human"),
            "live_view_url": _hide_navbar(status.get("live_view_url")),
            "active": True,
        }]

    # Stable ordering across polls (by each page's own id, which stays
    # fixed for that tab's lifetime) -- pages[]'s own array order isn't
    # documented as stable, and reordering tiles under someone's eyes every
    # 4s poll would be a jarring regression from the previous insertion-
    # ordered behavior.
    pages = sorted(pages, key=lambda p: p.get("id") or p.get("url") or "")

    known_by_url: dict[str, dict] = {}
    known_by_host: dict[str, dict] = {}

    def _track(url: str | None, entry: dict) -> None:
        if not url:
            return
        known_by_url[url] = entry
        host_key = _host_key(url)
        if host_key:
            # last-write-wins on a host+path collision (two tracked tabs
            # somehow resolve to the exact same key) -- acceptable, matches
            # this dict's existing exact-url behavior for the same edge
            # case. See _host_key's own docstring for why this is keyed on
            # host+path now, not bare host.
            known_by_host[host_key] = entry

    top_url = activity.get("url")
    if top_url:
        _track(top_url, {
            "heading": status.get("task"),
            "description": activity.get("description"),
            "steps": activity.get("steps") or [],
            "waiting_for_human": activity.get("waiting_for_human"),
            "tab_id": None,
        })
    for tab_id, tab in (activity.get("tabs") or {}).items():
        _track(tab.get("url"), {
            "heading": None,
            "description": tab.get("description"),
            "steps": tab.get("steps") or [],
            "waiting_for_human": tab.get("waiting_for_human"),
            "tab_id": tab_id,
        })

    active_tab_id = activity.get("active_tab_id")
    tiles = []
    for i, page in enumerate(pages):
        url = page.get("url")
        # Exact url match first (still the most precise when it happens to
        # line up); hostname match as the robust fallback -- see
        # _host_key's own docstring for why exact url matching alone was
        # silently losing every tile's live description/log.
        known = known_by_url.get(url) or known_by_host.get(_host_key(url))
        # Heading priority: the top-level tab's own overall-task heading
        # (only ever set via an exact-url match, see _track above) first;
        # then the REAL page title Browserbase reports for this exact page
        # (e.g. "Google Flights" vs "Google Hotels" -- distinct even though
        # both tabs share the google.com host, which a hostname-only
        # heading can't tell apart); hostname as the last resort for a
        # still-loading/blank page with no title yet.
        known_heading = known.get("heading") if known else None
        tiles.append({
            "id": page.get("id") or url or f"page-{i}",
            "heading": known_heading or _real_page_title(page) or _tile_heading(url),
            "description": known.get("description") if known else None,
            "steps": (known.get("steps") if known else None) or [],
            "waiting_for_human": known.get("waiting_for_human") if known else None,
            "live_view_url": _hide_navbar(page.get("debuggerFullscreenUrl") or page.get("debuggerUrl")),
            "active": bool(known and known.get("tab_id") == active_tab_id),
        })
    return tiles


# ---------------------------------------------------------------------------
# Dashboard page (second toggle-able page on /live/<token>): reminders,
# tasks, projects, contacts, and this week's schedule -- your own data, not
# tied to whether a browser is currently active. Reuses the SAME
# permanent live-share token as the browsing page (one link, two things it
# can show), resolved via db.get_user_by_live_token instead of
# get_live_status_by_token, since this has nothing to do with browsing
# state.
# ---------------------------------------------------------------------------

def _week_bounds_utc(user_tz: str, now: datetime | None = None) -> tuple[datetime, datetime]:
    """Monday 00:00 through the following Monday 00:00, in the user's OWN
    local timezone -- "this week" means something different depending on
    where you live, so this can't just be computed in UTC. Returned as a
    tz-aware UTC pair, ready to hand straight to
    db.list_calendar_events_for_range (Postgres/asyncpg compare tz-aware
    values correctly regardless of which aware zone they're expressed in)."""
    tz = ZoneInfo(user_tz)
    local_now = (now or datetime.now(ZoneInfo("UTC"))).astimezone(tz)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = local_midnight - timedelta(days=local_now.weekday())  # Monday
    week_end = week_start + timedelta(days=7)
    return week_start.astimezone(ZoneInfo("UTC")), week_end.astimezone(ZoneInfo("UTC"))


def _format_time_local(dt: datetime | None, tz: ZoneInfo) -> str:
    if dt is None:
        return ""
    local_dt = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    # "%-I" (no leading zero) isn't portable across platforms' libc --
    # strip a leading zero manually instead so this behaves the same on
    # any host this ends up deployed to.
    text = local_dt.strftime("%I:%M %p")
    return text[1:] if text.startswith("0") else text


def _format_due_local(dt: datetime | None, tz: ZoneInfo) -> str | None:
    """Short due-date label for a task ("Aug 30" or "Aug 30, 3:00 PM" when a
    real time-of-day was set, not just a bare date) -- None (not shown) when
    there's no due date at all."""
    if dt is None:
        return None
    local_dt = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    date_part = local_dt.strftime("%b %-d") if hasattr(local_dt, "strftime") else str(local_dt)
    if local_dt.hour == 0 and local_dt.minute == 0:
        return date_part
    return f"{date_part}, {_format_time_local(dt, tz)}"


@app.get("/live/{token}/dashboard")
async def live_view_dashboard(token: str) -> JSONResponse:
    """Polled by the dashboard page (the second toggle-able page on
    /live/<token> -- see live_view_page.py) on its own, slower cadence than
    the browsing status route, since tasks/reminders/projects/contacts/
    schedule change far less often than a live browsing session does. 404
    for an unknown token, same "don't let a bad link quietly look valid"
    reasoning as /live/<token>/status."""
    user = await db.get_user_by_live_token(token)
    if user is None:
        return JSONResponse({"error": "not found"}, status_code=404)

    user_id = user["id"]
    user_tz_name = user.get("timezone") or config.DEFAULT_TIMEZONE
    try:
        tz = ZoneInfo(user_tz_name)
    except Exception:  # noqa: BLE001 - an unrecognized/corrupt stored timezone must never break this page
        user_tz_name = config.DEFAULT_TIMEZONE
        tz = ZoneInfo(user_tz_name)

    week_start_utc, week_end_utc = _week_bounds_utc(user_tz_name)

    tasks, reminders, projects, contacts, week_events = await asyncio.gather(
        db.list_tasks(user_id),
        db.list_reminders(user_id, status="pending"),
        db.list_projects(user_id, status="active"),
        db.list_people(user_id),
        db.list_calendar_events_for_range(user_id, week_start_utc, week_end_utc),
    )

    days = []
    for i in range(7):
        day_start_local = week_start_utc.astimezone(tz) + timedelta(days=i)
        days.append({
            "date": day_start_local.strftime("%Y-%m-%d"),
            "label": day_start_local.strftime("%A, %b %-d"),
            "is_today": day_start_local.date() == datetime.now(tz).date(),
            "events": [],
        })
    for ev in week_events:
        start = ev.get("start_time")
        if start is None:
            continue
        local_start = start.astimezone(tz) if start.tzinfo else start.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
        day_index = (local_start.date() - week_start_utc.astimezone(tz).date()).days
        if 0 <= day_index < 7:
            days[day_index]["events"].append({
                "id": ev.get("id"),
                "time": _format_time_local(start, tz),
                "end_time": _format_time_local(ev.get("end_time"), tz) or None,
                "title": ev.get("title") or "Untitled event",
                "location": ev.get("location"),
            })

    return JSONResponse({
        "name": user.get("name"),
        "week": {
            "label": f"{days[0]['label']} – {days[6]['label']}, {week_start_utc.astimezone(tz).year}",
            "days": days,
        },
        "tasks": [
            {
                "id": t.get("id"),
                "title": t.get("title"),
                "status": t.get("status"),
                "priority": t.get("priority"),
                "due": _format_due_local(t.get("due_date"), tz),
            }
            for t in tasks
        ],
        "reminders": [
            {
                "id": r.get("id"),
                "message": r.get("message"),
                "time": _format_due_local(r.get("trigger_time"), tz),
            }
            for r in reminders
        ],
        "projects": [
            {"id": p.get("id"), "title": p.get("title"), "status": p.get("status")}
            for p in projects
        ],
        "contacts": [
            {
                "id": c.get("id"),
                "name": c.get("name"),
                "relationship": c.get("relationship_type"),
                "notes": c.get("notes"),
            }
            for c in contacts
        ],
    })


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


async def _production_deepsearch_pause_loop() -> None:
    """Notifies you when deepsearch is waiting on a login/CAPTCHA/2FA page
    (see tools/deepsearch_tools.py's request_human_help and db.py's
    deepsearch_human_help_requests functions) -- same shape as the reminder/
    cron loops above, but polled much more often (5s vs 30s by default,
    config.DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS), since a stalled
    login is time-sensitive in a way a reminder isn't.

    Deliberately sends ONE fixed-template SMS, not a re-invoked Messa LLM
    call -- request_human_help's own wait/extend/give-up state machine runs
    entirely inside the in-flight deepsearch call; this loop's only job is
    "notice a new waiting row, text the user once, remember not to text
    again." A row keeps showing up in get_waiting_human_help_requests on
    every poll until request_human_help itself resolves or times it out --
    notified_at (checked here, set by mark_human_help_notified) is what
    keeps that from becoming a text every 5 seconds."""
    while True:
        try:
            waiting = await db.get_waiting_human_help_requests()
            for req in waiting:
                if req.get("notified_at") is not None:
                    continue
                link = None
                try:
                    token = await db.get_or_create_live_share_token(req["user_id"])
                    if token and config.LIVE_VIEW_BASE_URL:
                        link = f"{config.LIVE_VIEW_BASE_URL}/live/{token}"
                except Exception as e:  # noqa: BLE001 - the SMS is still worth sending without a link
                    console.system(f"[deepsearch pause notify] link lookup failed: {e}")
                text = f"Hey, I need your help finishing up -- {req['reason']}."
                if link:
                    text += f" Jump into the live view when you get a sec: {link}"
                try:
                    await sendblue.send_message(req["phone_number"], text)
                except SendblueError as e:
                    console.system(f"[deepsearch pause notify failed] request=#{req['id']}: {e}")
                    continue
                await db.mark_human_help_notified(req["id"])
        except Exception as e:  # noqa: BLE001 - a background loop must never die silently mid-process
            console.system(f"[deepsearch pause poller error] {e}")
        await asyncio.sleep(config.DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS)


@app.on_event("startup")
async def _startup() -> None:
    global _bg_tasks
    _bg_tasks = [
        asyncio.create_task(_production_reminder_loop()),
        asyncio.create_task(_production_cron_loop()),
        asyncio.create_task(_production_deepsearch_pause_loop()),
    ]
    console.system("Started production reminder/cron/deepsearch-pause delivery pollers.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    for t in _bg_tasks:
        t.cancel()
    await db.close_pool()
