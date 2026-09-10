"""Routines subagent: recurring AND one-shot task routines (briefings,
recurring check-ins, autonomous watchers, deadline-aware follow-ups, etc).

Creating a routine is gated (it's in the DB's action_type set as
"create_routine" -- only Messa can confirm the proposal). Pausing,
resuming, cancelling, rescheduling, or self-finishing an existing routine
happens directly since none of those create new standing automation
(removing/adjusting something already-approved is lower risk than creating
new automation from scratch).

Actually *running* a due routine (re-invoking Messa with `prompt_or_task`
on schedule, or sending a plain reminder) needs a long-lived backend
process, which the interactive CLI isn't -- so messa/background.py's local
poller only ever prints what *would* fire. server.py's `_production_cron_loop`
is the real counterpart: running as part of the always-on webhook server, it
actually fires routines and delivers via Sendblue (or, for a digest-tagged
routine, queues the result for the next digest flush -- see
_production_digest_loop).

--- execution_mode: the "reminder for the user" vs "task for Messa" split ---

Every routine is either:
  - 'notify': a plain reminder. THE USER is going to do the thing; Messa's
    only job is to say the reminder text at the right time. No agent turn
    runs at all when this fires -- it's a direct, deterministic text send
    (see server.py), so there's no risk of Messa "helpfully" going off and
    doing the task herself when the user only asked to be reminded.
  - 'autonomous': MESSA does the thing herself, using her full toolset, and
    reports back. This is what actually re-invokes a full agent turn on
    schedule -- browsing, checking email, whatever the saved prompt_or_task
    actually asks for.

Both "remind me to check Gmail" and "check my Gmail and summarize it" can
be phrased almost identically in plain English -- the distinction is in
WHO is expected to act, not in the words used. Pick 'notify' when the
user's own words describe THEM doing something (check, call, go, pay,
show up) and the point of the message is just to prompt them at the right
time. Pick 'autonomous' when the words describe MESSA doing something on
the user's behalf (check X and tell me, keep an eye on Y, watch for Z and
let me know / go ahead and do it) -- i.e. anything that needs her to
actually use a tool (browse, read email, etc) when it fires, not just say
a sentence. When genuinely unclear, default to 'notify' -- it's the
cheaper, safer failure mode (worst case, the user says "no, I meant you
should check it yourself" and you recreate it as 'autonomous'). Either
way, the proposal text sent back for confirmation should say which one in
plain language ("I'll remind you to..." vs "I'll check ... myself and text
you...") so a wrong read gets caught before it's confirmed.

Everything else in this module -- watchers, deadlines, digests, retries,
escalation -- is just optional shape layered onto these two primitives, not
a separate system. In particular, a CONDITIONAL plan ("if it drops under
$50 buy it, otherwise tell me Friday") needs no special modeling at all:
it's 'autonomous' mode, and the condition/branches are just part of the
natural-language prompt_or_task -- Messa reads and acts on it fresh, with
her full judgment and toolset, every time the routine fires, exactly like
any other instruction.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import BaseTool, tool

from .. import config, db, reliability
from .common import last_ai_text, run_inner_agent_with_claim_check, trace_all
from .integration_circuit_breaker import ToolFailureLadderMiddleware
from .scratchpad_tools import build_scratchpad_tools, scratchpad_prompt_block

LABEL = "routines_agent"

VALID_EXECUTION_MODES = ("notify", "autonomous")
ONE_SHOT_SENTINEL = "once"  # stored in cron_expression for a one-time (non-recurring) routine


def compute_next_run(cron_expression: str, tz_name: str, base: datetime | None = None) -> datetime:
    tz = ZoneInfo(tz_name)
    base = (base or datetime.now(tz)).astimezone(tz)
    return croniter(cron_expression, base).get_next(datetime)


def _format_job_line(r: dict) -> str:
    mode = r.get("execution_mode") or "autonomous"
    when = ONE_SHOT_SENTINEL if r.get("cron_expression") == ONE_SHOT_SENTINEL else r.get("cron_expression")
    bits = [f"#{r['id']} [{r['status']}/{mode}]", f"'{r['prompt_or_task']}'", f"({when}, next: {r['next_run_at']})"]
    meta_raw = r.get("meta")
    if meta_raw:
        import json as _json
        try:
            meta = _json.loads(meta_raw) if isinstance(meta_raw, str) else meta_raw
        except (TypeError, ValueError):
            meta = {}
        extras = []
        if meta.get("deadline_at"):
            extras.append(f"deadline {meta['deadline_at']}")
        if meta.get("digest"):
            extras.append("digest")
        if meta.get("expire_at"):
            extras.append(f"expires {meta['expire_at']}")
        if meta.get("last_run_status"):
            extras.append(f"last run: {meta['last_run_status']}")
        if meta.get("ended_reason"):
            extras.append(f"ended: {meta['ended_reason']}")
        if extras:
            bits.append("[" + ", ".join(extras) + "]")
    return " ".join(bits)


def _build_schedule_and_meta(
    user: config.UserContext,
    *,
    cron_expression: str | None,
    run_once_in_minutes: int | None,
    deadline_in_minutes: int | None,
    expire_in_hours: float | None,
    digest: bool,
    escalate_on_no_response: bool,
    apply_expiry: bool,
    default_expire_hours: float,
    max_expire_hours: float,
    allow_no_expiry: bool,
) -> tuple[datetime, str, str, dict] | str:
    """Shared by propose_create_routine and propose_create_project_capsule
    (V3-autonomous.md Phase 6) -- both are "pick a schedule, build a meta
    dict" operations that differ only in WHICH expiry bounds apply (a
    project capsule gets its own, longer-but-still-bounded ceiling --
    config.PROJECT_DEFAULT_EXPIRE_HOURS/PROJECT_MAX_EXPIRE_HOURS -- instead
    of an ordinary routine's) and in whether the 0-hours "run forever"
    escape hatch exists at all (a project capsule never gets it). Pulled
    out rather than duplicated so the two proposal tools can't quietly
    drift out of sync with each other over time.

    Returns (next_run, stored_cron_expression, schedule_desc, meta) on
    success, or an "ERROR: ..." string a caller should return verbatim."""
    if bool(cron_expression) == bool(run_once_in_minutes):
        return "ERROR: give exactly one of cron_expression or run_once_in_minutes, not both/neither."

    now_local = datetime.now(ZoneInfo(user.timezone))
    if cron_expression:
        try:
            next_run = compute_next_run(cron_expression, user.timezone, base=now_local)
        except Exception as e:
            return f"ERROR: '{cron_expression}' isn't a valid cron expression ({e})."
        stored_cron_expression = cron_expression
        schedule_desc = f"on schedule '{cron_expression}'"
    else:
        if run_once_in_minutes <= 0:
            return "ERROR: run_once_in_minutes must be positive."
        next_run = now_local + timedelta(minutes=run_once_in_minutes)
        stored_cron_expression = ONE_SHOT_SENTINEL
        schedule_desc = f"once, in {run_once_in_minutes} minutes ({next_run.isoformat()})"

    meta: dict = {}
    if deadline_in_minutes is not None:
        if deadline_in_minutes <= 0:
            return "ERROR: deadline_in_minutes must be positive."
        meta["deadline_at"] = (now_local + timedelta(minutes=deadline_in_minutes)).isoformat()
    if apply_expiry:
        if not allow_no_expiry and expire_in_hours == 0:
            return "ERROR: expire_in_hours must be positive for this kind of task (there's no 'run forever' option)."
        if not (allow_no_expiry and expire_in_hours == 0):
            # 0 (only reachable when allow_no_expiry) is the explicit,
            # documented "no expiry -- genuinely ongoing" opt-out; None
            # (the common case, nothing specified) gets the safe bounded
            # default rather than silently running forever.
            hours = expire_in_hours if expire_in_hours is not None else default_expire_hours
            hours = min(hours, max_expire_hours)
            if hours < 0:
                suffix = " (or 0 for no expiry)." if allow_no_expiry else "."
                return f"ERROR: expire_in_hours must be positive{suffix}"
            meta["expire_at"] = (now_local + timedelta(hours=hours)).isoformat()
    if digest:
        meta["digest"] = True
    if escalate_on_no_response:
        meta["escalate_on_no_response"] = True
    return next_run, stored_cron_expression, schedule_desc, meta


def build_routines_tools(user: config.UserContext) -> list[BaseTool]:
    uid = user.user_id

    @tool
    async def list_cron_jobs() -> str:
        """List the user's routines (active, paused, and cancelled) -- both
        reminders and autonomous tasks, recurring and one-shot -- including
        mode, next run, and any deadline/digest/retry state. Use this any
        time the user asks what you're keeping an eye on for them, or before
        rescheduling/cancelling one to find its id."""
        rows = await db.list_cron_jobs(uid)
        if not rows:
            return "No routines set up."
        return "\n".join(_format_job_line(r) for r in rows)

    @tool
    async def propose_create_routine(
        prompt_or_task: str,
        execution_mode: str,
        cron_expression: str | None = None,
        run_once_in_minutes: int | None = None,
        deadline_in_minutes: int | None = None,
        expire_in_hours: float | None = None,
        digest: bool = False,
        escalate_on_no_response: bool = False,
    ) -> str:
        """Propose a new routine. This only stages it for confirmation.

        execution_mode: 'notify' (a plain reminder -- the USER does the
        thing) or 'autonomous' (MESSA does the thing herself, using her
        tools, and reports back). See this module's own docstring for how
        to pick.

        Schedule -- give EXACTLY ONE of:
        - cron_expression: standard 5-field cron ('minute hour day month
          weekday'), evaluated in the user's timezone, for something
          recurring. Example: '0 8 * * *' = daily at 8am.
        - run_once_in_minutes: fires exactly once, this many minutes from
          now, then retires itself. Use this for "check back in a few
          hours" / "remind me at 4pm today" style one-off requests -- not
          everything needs to repeat forever.

        deadline_in_minutes (optional): if this routine is working toward a
        deadline (an event, "by Friday", etc), set how many minutes from now
        that deadline is. 'notify' routines pace their own check-ins against
        it (a light nudge early, more direct as it nears); 'autonomous'
        watchers use it as a natural stopping point.

        expire_in_hours (optional, 'autonomous' only): hard stop for a
        self-checking watcher -- after this long with no resolution, it
        gives up and tells the user instead of continuing to check forever.
        Defaults to a sane bound if omitted -- ALWAYS omit it (or set it
        explicitly) rather than guessing high, since the default only kicks
        in when nothing is given. Pass 0 ONLY for something genuinely meant
        to run indefinitely with no natural end (e.g. "check my Gmail every
        weekday morning") -- not for anything you'd describe as watching,
        checking on, or following up until something happens, which should
        always have a real bound. Can't exceed the platform max regardless
        of what's asked for.

        digest (optional): if true, this routine's result is queued into
        the next "while you were away" batch instead of texting the instant
        it resolves -- good for something that might resolve overnight.

        escalate_on_no_response (optional): if true and the user doesn't
        respond after a check-in, follow-ups get a little more frequent and
        direct (bounded so it never turns into spam) instead of holding a
        fixed pace regardless of silence.
        """
        if execution_mode not in VALID_EXECUTION_MODES:
            return f"ERROR: execution_mode must be one of {VALID_EXECUTION_MODES}, got {execution_mode!r}."

        result = _build_schedule_and_meta(
            user,
            cron_expression=cron_expression, run_once_in_minutes=run_once_in_minutes,
            deadline_in_minutes=deadline_in_minutes, expire_in_hours=expire_in_hours,
            digest=digest, escalate_on_no_response=escalate_on_no_response,
            apply_expiry=(execution_mode == "autonomous"),
            default_expire_hours=config.ROUTINE_DEFAULT_EXPIRE_HOURS,
            max_expire_hours=config.ROUTINE_MAX_EXPIRE_HOURS,
            allow_no_expiry=True,
        )
        if isinstance(result, str):
            return result
        next_run, stored_cron_expression, schedule_desc, meta = result

        row = await db.propose_action(
            uid, "create_routine",
            {
                "prompt_or_task": prompt_or_task,
                "cron_expression": stored_cron_expression,
                "user_timezone": user.timezone,
                "next_run_at": next_run,
                "execution_mode": execution_mode,
                "meta": meta,
            },
        )
        mode_desc = "remind you" if execution_mode == "notify" else "check this myself and text you"
        return (
            f"Proposed (pending confirmation, id #{row['id']}): I'll {mode_desc} -- "
            f"'{prompt_or_task}', {schedule_desc}."
        )

    @tool
    async def pause_cron_job(cron_id: int) -> str:
        """Pause a routine by id (stops firing until resumed)."""
        row = await db.set_cron_job_status(uid, cron_id, "paused")
        return f"Paused job #{cron_id}." if row else f"No job #{cron_id} found."

    @tool
    async def resume_cron_job(cron_id: int) -> str:
        """Resume a paused routine by id."""
        row = await db.set_cron_job_status(uid, cron_id, "active")
        return f"Resumed job #{cron_id}." if row else f"No job #{cron_id} found."

    @tool
    async def cancel_cron_job(cron_id: int) -> str:
        """Cancel a routine by id permanently."""
        row = await db.set_cron_job_status(uid, cron_id, "cancelled", {"ended_reason": "cancelled_by_user"})
        return f"Cancelled job #{cron_id}." if row else f"No job #{cron_id} found."

    @tool
    async def reschedule_routine(cron_id: int, delay_minutes: int) -> str:
        """Push a routine's next run back by delay_minutes from now -- for a
        plain-language reschedule reply ("not yet, ask me again in an
        hour"). Use list_cron_jobs first if you need to figure out which id
        the user means (usually the most recently mentioned/notified one).
        Works on both 'notify' and 'autonomous' routines, one-shot or
        recurring; doesn't change the routine's status."""
        if delay_minutes <= 0:
            return "ERROR: delay_minutes must be positive."
        rows = await db.list_cron_jobs(uid)
        job = next((r for r in rows if r["id"] == cron_id), None)
        if job is None:
            return f"No job #{cron_id} found."
        next_run = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        await db.reschedule_cron_job(cron_id, next_run)
        return f"Rescheduled job #{cron_id} to run again in {delay_minutes} minutes."

    @tool
    async def finish_routine(cron_id: int, outcome_summary: str) -> str:
        """Call this from WITHIN an autonomous routine's own firing (when
        you're re-checking on something in the background) once the task is
        actually done and no more automatic checks are needed -- e.g. the
        tickets you were watching for showed up and you handled them, or
        booked/paid as authorized. This stops the routine from firing again.
        Do NOT call this for a routine that's still ongoing -- if it's not
        resolved yet, just let this run end normally and it'll check again
        on its own schedule."""
        rows = await db.list_cron_jobs(uid)
        job = next((r for r in rows if r["id"] == cron_id), None)
        if job is None:
            return f"No job #{cron_id} found."
        await db.set_cron_job_status(
            uid, cron_id, "cancelled",
            {"ended_reason": "completed_by_agent", "outcome": outcome_summary},
        )
        return f"Finished and stopped job #{cron_id}: {outcome_summary}"

    @tool
    async def propose_create_project_capsule(
        title: str,
        goal: str,
        cadence_cron_expression: str,
        expire_in_hours: float | None = None,
        deadline_in_minutes: int | None = None,
        digest: bool = False,
        escalate_on_no_response: bool = False,
    ) -> str:
        """Propose a new PROJECT CAPSULE: a delegated, multi-day/multi-week
        OBJECTIVE that you manage autonomously on your own cadence until
        it's resolved -- an airline refund dispute, an apartment search, an
        investor outreach campaign, a holiday gift hunt. Use this instead
        of a plain propose_create_routine when the user is handing off a
        whole GOAL with many possible steps and an uncertain path to done,
        not a single recurring check. This only stages it for confirmation,
        same as propose_create_routine.

        title: short label for the project (e.g. "Delta $850 refund dispute").
        goal: the actual objective in plain language -- exactly what you'll
        act on every time the cadence check fires.
        cadence_cron_expression: standard 5-field cron for how often you
        check in on this in the background (e.g. '0 9 * * *' = daily at
        9am). Pick a cadence that matches how fast this kind of thing
        actually moves -- daily for something time-sensitive, every few
        days for a slower search.
        expire_in_hours (optional): hard stop for the whole project --
        after this long with no resolution, you give up and tell the user
        instead of continuing indefinitely. Defaults to a generous bound
        for a genuine multi-week project (and can't exceed the platform
        max regardless of what's asked for). Unlike an ordinary routine, a
        project has no "run forever" option -- always give it a real,
        if generous, end.
        deadline_in_minutes, digest, escalate_on_no_response: same meaning
        as propose_create_routine's own version of each.
        """
        if not title.strip() or not goal.strip():
            return "ERROR: title and goal can't be empty."
        if len(title) > config.PROJECT_CAPSULE_TITLE_MAX_LENGTH:
            return (
                f"ERROR: title is too long ({len(title)} characters, max "
                f"{config.PROJECT_CAPSULE_TITLE_MAX_LENGTH}) -- shorten it to a short label, "
                "put any extra detail in `goal` instead."
            )
        result = _build_schedule_and_meta(
            user,
            cron_expression=cadence_cron_expression, run_once_in_minutes=None,
            deadline_in_minutes=deadline_in_minutes, expire_in_hours=expire_in_hours,
            digest=digest, escalate_on_no_response=escalate_on_no_response,
            apply_expiry=True,
            default_expire_hours=config.PROJECT_DEFAULT_EXPIRE_HOURS,
            max_expire_hours=config.PROJECT_MAX_EXPIRE_HOURS,
            allow_no_expiry=False,
        )
        if isinstance(result, str):
            return result
        next_run, stored_cron_expression, schedule_desc, meta = result
        row = await db.propose_action(
            uid, "create_project",
            {
                "title": title,
                "prompt_or_task": goal,
                "cron_expression": stored_cron_expression,
                "user_timezone": user.timezone,
                "next_run_at": next_run,
                "execution_mode": "autonomous",
                "meta": meta,
            },
        )
        return (
            f"Proposed (pending confirmation, id #{row['id']}): I'll manage the project "
            f"'{title}' -- checking in {schedule_desc}, on: {goal}"
        )

    @tool
    async def list_my_project_capsules() -> str:
        """List the user's project capsules (delegated multi-step objectives
        you're managing autonomously) -- active, paused, completed, and
        cancelled -- with each one's status and next check-in. Use this
        before filing a vault asset or pulling up a project's details, to
        find its id."""
        rows = await db.list_project_capsules(uid)
        if not rows:
            return "No project capsules set up."
        lines = []
        for r in rows:
            bits = [f"#{r['id']} [{r['status']}]", f"'{r['title']}'"]
            if r.get("next_run_at"):
                bits.append(f"(next check-in: {r['next_run_at']})")
            lines.append(" ".join(bits))
        return "\n".join(lines)

    @tool
    async def get_project_capsule_details(project_id: int) -> str:
        """Full detail on one project capsule: its goal, status, vault
        assets on file, and recent timeline events. Call this before an
        autonomous cadence check-in on a project so you know what's
        already been done and what's already in the vault -- and before
        deciding whether an incoming photo/PDF plausibly belongs in it."""
        project = await db.get_project_capsule(uid, project_id)
        if project is None:
            return f"No project capsule #{project_id} found."
        assets = await db.list_project_capsule_assets(project_id)
        events = await db.list_project_capsule_events(project_id)
        lines = [
            f"#{project['id']} '{project['title']}' [{project['status']}]",
            f"Goal: {project['goal']}",
        ]
        if project.get("outcome_summary"):
            lines.append(f"Outcome: {project['outcome_summary']}")
        lines.append(
            "Vault: " + (
                "; ".join(f"{a['description'] or '(no description)'} ({a['source_url']})" for a in assets)
                if assets else "empty."
            )
        )
        if events:
            lines.append("Recent timeline:")
            lines.extend(f"  - {e['created_at']}: {e['event_text']}" for e in events[:10])
        return "\n".join(lines)

    @tool
    async def pause_project_capsule(project_id: int) -> str:
        """Pause a project capsule's autonomous cadence checks (stops
        firing until resumed) without losing its vault or timeline."""
        project = await db.get_project_capsule(uid, project_id)
        if project is None or not project.get("cron_job_id"):
            return f"No project capsule #{project_id} found."
        await db.set_cron_job_status(uid, project["cron_job_id"], "paused")
        return f"Paused project capsule #{project_id}."

    @tool
    async def resume_project_capsule(project_id: int) -> str:
        """Resume a paused project capsule's autonomous cadence checks."""
        project = await db.get_project_capsule(uid, project_id)
        if project is None or not project.get("cron_job_id"):
            return f"No project capsule #{project_id} found."
        await db.set_cron_job_status(uid, project["cron_job_id"], "active")
        return f"Resumed project capsule #{project_id}."

    @tool
    async def cancel_project_capsule(project_id: int) -> str:
        """Cancel a project capsule permanently -- the user gave up on it or
        no longer wants it tracked. Distinct from finish_project_capsule,
        which is for an objective that's actually been achieved."""
        project = await db.get_project_capsule(uid, project_id)
        if project is None:
            return f"No project capsule #{project_id} found."
        if project.get("cron_job_id"):
            # set_cron_job_status's own project-capsule sync marks this
            # 'cancelled' (not 'completed') and logs the timeline event --
            # nothing more to do here.
            await db.set_cron_job_status(
                uid, project["cron_job_id"], "cancelled", {"ended_reason": "cancelled_by_user"},
            )
        else:
            # No linked cron job to change the status of (e.g. it was
            # deleted out from under this capsule) -- direct status-only
            # cancel. Deliberately NOT finish_project_capsule, which always
            # marks 'completed': a cancelled-with-nothing-achieved project
            # must never look like a successfully finished one.
            await db.cancel_orphaned_project_capsule(uid, project_id)
            await db.log_project_capsule_event(project_id, "Cancelled by the user.")
        return f"Cancelled project capsule #{project_id}."

    @tool
    async def finish_project_capsule(project_id: int, outcome_summary: str) -> str:
        """Call this when a project capsule's goal has actually been
        achieved (the refund came through, the apartment got signed) --
        typically from WITHIN the project's own autonomous cadence
        check-in. Stops future check-ins and records the outcome. Do NOT
        call this if the project is just quiet/no news this cycle -- let
        it check again on its own schedule."""
        row = await db.finish_project_capsule(uid, project_id, outcome_summary)
        if not row:
            return f"No project capsule #{project_id} found."
        # No manual log_project_capsule_event call here: when this capsule
        # has a linked cron job, db.finish_project_capsule already routed
        # through set_cron_job_status, whose own project-capsule sync logs
        # the completion timeline event exactly once -- a second call here
        # would double it.
        return f"Finished project capsule #{project_id}: {outcome_summary}"

    @tool
    async def add_project_capsule_asset(project_id: int, source_url: str, description: str = "") -> str:
        """File a user-supplied photo/PDF/document into a project capsule's
        vault. Use ONLY when the user just sent an attachment that
        plausibly belongs to an active project capsule -- an inbound
        message carrying one is tagged with its own [attachment_url: ...];
        pass that exact URL here. Use your own judgment: an ambiguous or
        unrelated attachment should just be asked about or left alone, not
        filed automatically. Also logs a timeline event."""
        project = await db.get_project_capsule(uid, project_id)
        if project is None:
            return f"No project capsule #{project_id} found."
        await db.add_project_capsule_asset(project_id, source_url, description or None)
        await db.log_project_capsule_event(project_id, f"Filed a new vault item: {description or source_url}")
        return f"Filed into project capsule #{project_id}'s vault."

    @tool
    async def log_project_capsule_event(project_id: int, event_text: str) -> str:
        """Record a short, plain-language timeline entry for a project
        capsule -- what you just did or found out (e.g. 'Emailed Delta
        support asking for a status update on the refund'). Skip this on a
        cadence check-in that found nothing new to report -- don't log a
        no-op just to have logged something."""
        project = await db.get_project_capsule(uid, project_id)
        if project is None:
            return f"No project capsule #{project_id} found."
        await db.log_project_capsule_event(project_id, event_text)
        return "Logged."

    raw_tools: list[BaseTool] = [
        list_cron_jobs, propose_create_routine, pause_cron_job, resume_cron_job,
        cancel_cron_job, reschedule_routine, finish_routine,
    ]
    if config.PROJECT_CAPSULES_ENABLED:
        raw_tools += [
            propose_create_project_capsule, list_my_project_capsules, get_project_capsule_details,
            pause_project_capsule, resume_project_capsule, cancel_project_capsule,
            finish_project_capsule, add_project_capsule_asset, log_project_capsule_event,
        ]
    return trace_all(raw_tools, LABEL)


ROUTINES_SYSTEM_PROMPT = (
    "You are the routines specialist: recurring AND one-time task routines (daily "
    "briefings, weekly check-ins, background watchers, deadline-aware follow-ups), "
    "delegated to you by Messa.\n"
    "- Every routine is either 'notify' (a plain reminder -- the USER does the thing) or "
    "'autonomous' (MESSA does the thing herself and reports back). Read propose_create_routine's "
    "own docstring for exactly how to tell these apart -- getting this right matters: a 'notify' "
    "routine is a cheap, guaranteed-exact text send with no model involved when it fires, while "
    "an 'autonomous' one re-runs you with your full toolset. When genuinely unclear, default to "
    "'notify' and let the user correct you if they actually wanted you to do it yourself.\n"
    "- Creating a new routine goes through propose_create_routine -- it only stages it. Tell "
    "Messa what was proposed (which mode you picked, in plain words) and its pending id so the "
    "user can confirm.\n"
    "- Pausing, resuming, cancelling, or rescheduling an existing routine happens immediately, "
    "no confirmation needed. Use reschedule_routine when a user replies to a reminder/check-in "
    "with something like 'not yet, ask me later' or 'check again in an hour'.\n"
    "- Once confirmed, a routine actually fires on schedule in production (the always-on webhook "
    "server runs it) -- it's not just stored for show. The interactive CLI's own preview only "
    "prints what would fire, since it isn't a long-lived process.\n"
    "- If you (Messa) are CURRENTLY running because an autonomous routine just fired (you'll see "
    "this called out explicitly in the message that started this turn, including the routine's "
    "own id), and what you find means the task is fully done, call finish_routine so it stops "
    "checking again automatically. If it's not resolved yet, just answer normally -- it'll fire "
    "again on its own.\n"
)

# Spliced onto ROUTINES_SYSTEM_PROMPT only when config.PROJECT_CAPSULES_ENABLED
# (see build_routines_subagent below) -- kept in its own short constant,
# not folded into ROUTINES_SYSTEM_PROMPT unconditionally, so the ordinary
# routines-only prompt stays exactly as lean as it was before this feature
# existed on any deployment that has it turned off.
PROJECT_CAPSULES_SYSTEM_PROMPT = (
    "- A PROJECT CAPSULE (propose_create_project_capsule) is for a whole delegated, "
    "multi-day/multi-week OBJECTIVE with an uncertain path to done (a refund dispute, an "
    "apartment search, an outreach campaign) -- not a single recurring check. It's still an "
    "'autonomous' routine underneath, just wrapped with its own vault (file an attachment via "
    "add_project_capsule_asset only when it plausibly belongs, using your own judgment -- never "
    "automatically) and timeline (log_project_capsule_event, kept short). Call "
    "get_project_capsule_details first on a cadence check-in so you know what's already been "
    "done. Stay low-noise: on a check-in with genuinely nothing new, keep your reply short "
    "rather than padding it out. Call finish_project_capsule only once the actual goal is "
    "achieved, same convention as finish_routine.\n"
)


def build_routines_subagent(user: config.UserContext, model: BaseChatModel) -> dict[str, Any]:
    """Returns a deepagents `CompiledSubAgent` spec (feature/agentic-upgrade
    plan) -- same shape/reasoning as email_tools.build_email_subagent.
    routines_agent used to be a plain declarative SubAgent dict (tools/
    system_prompt frozen at build_orchestrator's start) -- see
    tools/integration_tools.py's build_integration_subagent docstring for
    the concrete "artifact created earlier this turn is invisible to a
    later delegation" bug this fixes."""

    async def _run(state: dict[str, Any]) -> dict[str, Any]:
        messages = list(state["messages"])
        tools = build_routines_tools(user)
        system_prompt = ROUTINES_SYSTEM_PROMPT
        if config.PROJECT_CAPSULES_ENABLED:
            system_prompt = system_prompt + PROJECT_CAPSULES_SYSTEM_PROMPT
        system_prompt = system_prompt + reliability.RELIABILITY_GUARDRAIL_STR
        if config.SCRATCHPAD_AND_SKILLS_ENABLED:
            tools = tools + build_scratchpad_tools(user, "routines_agent")
            system_prompt = system_prompt + await scratchpad_prompt_block(user, "routines_agent")
        run_config = {"recursion_limit": config.RECURSION_LIMIT}
        inner_agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            middleware=[ToolFailureLadderMiddleware("propose_create_routine")],
        )
        final_messages = await run_inner_agent_with_claim_check(
            inner_agent, messages, run_config, label="routines_agent"
        )
        return {"messages": [AIMessage(content=last_ai_text(final_messages))]}

    description = (
        "Sets up, lists, pauses, resumes, cancels, and reschedules task routines -- "
        "both recurring and one-time, both plain reminders (the user does something) "
        "and autonomous background tasks (you do something yourself later and report "
        "back, e.g. watchers, deadline-aware follow-ups, digests). Use for anything "
        "recurring/scheduled, or anything you're telling the user you'll check on or "
        "follow up about later."
    )
    if config.PROJECT_CAPSULES_ENABLED:
        description += (
            " Also owns PROJECT CAPSULES: delegated multi-day/multi-week objectives with "
            "their own vault and timeline (an airline refund dispute, an apartment search, "
            "an outreach campaign) -- use when the user hands off a whole open-ended goal "
            "to manage and walk away from, not just a single scheduled check."
        )

    return {
        "name": "routines_agent",
        "description": description,
        "runnable": RunnableLambda(_run),
    }
