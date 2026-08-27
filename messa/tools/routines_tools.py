"""Routines subagent: recurring cron jobs (briefings, recurring check-ins, etc).

Creating a cron job is gated (it's in the DB's action_type enum as
create_recurring_cron -- only Messa can confirm the proposal). Pausing or
cancelling an existing job is direct since the enum has no separate type
for that (removing something already-approved is lower risk than creating
new standing automation).

Actually *running* a due cron job (i.e. re-invoking Messa with
`prompt_or_task` on schedule) is Phase 2's job -- it needs a long-lived
backend process, which the interactive CLI isn't. Phase 1 only wires up the
CRUD + a local poller (see messa/background.py) that prints what *would*
fire, so the concept is testable end-to-end before the real scheduler
exists.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter
from langchain_core.tools import BaseTool, tool

from .. import config, db
from .common import trace_all

LABEL = "routines_agent"


def compute_next_run(cron_expression: str, tz_name: str, base: datetime | None = None) -> datetime:
    tz = ZoneInfo(tz_name)
    base = (base or datetime.now(tz)).astimezone(tz)
    return croniter(cron_expression, base).get_next(datetime)


def build_routines_tools(user: config.UserContext) -> list[BaseTool]:
    uid = user.user_id

    @tool
    async def list_cron_jobs() -> str:
        """List the user's recurring jobs (active, paused, and cancelled)."""
        rows = await db.list_cron_jobs(uid)
        if not rows:
            return "No recurring jobs set up."
        return "\n".join(
            f"#{r['id']} [{r['status']}] '{r['prompt_or_task']}' ({r['cron_expression']}, "
            f"next: {r['next_run_at']})"
            for r in rows
        )

    @tool
    async def propose_create_recurring_cron(prompt_or_task: str, cron_expression: str) -> str:
        """Propose a new recurring job. cron_expression is standard 5-field cron
        ('minute hour day month weekday'), evaluated in the user's timezone.
        Examples: '0 8 * * *' = daily at 8am. This only stages it for confirmation."""
        try:
            next_run = compute_next_run(cron_expression, user.timezone)
        except Exception as e:
            return f"ERROR: '{cron_expression}' isn't a valid cron expression ({e})."
        row = await db.propose_action(
            uid, "create_recurring_cron",
            {
                "prompt_or_task": prompt_or_task,
                "cron_expression": cron_expression,
                "user_timezone": user.timezone,
                "next_run_at": next_run,
            },
        )
        return (
            f"Proposed (pending confirmation, id #{row['id']}): recurring job '{prompt_or_task}' "
            f"on schedule '{cron_expression}', next run {next_run.isoformat()}."
        )

    @tool
    async def pause_cron_job(cron_id: int) -> str:
        """Pause a recurring job by id (stops firing until resumed)."""
        row = await db.set_cron_job_status(uid, cron_id, "paused")
        return f"Paused job #{cron_id}." if row else f"No job #{cron_id} found."

    @tool
    async def resume_cron_job(cron_id: int) -> str:
        """Resume a paused recurring job by id."""
        row = await db.set_cron_job_status(uid, cron_id, "active")
        return f"Resumed job #{cron_id}." if row else f"No job #{cron_id} found."

    @tool
    async def cancel_cron_job(cron_id: int) -> str:
        """Cancel a recurring job by id permanently."""
        row = await db.set_cron_job_status(uid, cron_id, "cancelled")
        return f"Cancelled job #{cron_id}." if row else f"No job #{cron_id} found."

    raw_tools: list[BaseTool] = [
        list_cron_jobs, propose_create_recurring_cron, pause_cron_job, resume_cron_job, cancel_cron_job,
    ]
    return trace_all(raw_tools, LABEL)


ROUTINES_SYSTEM_PROMPT = (
    "You are the routines specialist: recurring automations (daily briefings, weekly "
    "check-ins, etc), delegated to you by Messa.\n"
    "- Creating a new recurring job goes through propose_create_recurring_cron -- it only "
    "stages the job. Tell Messa what was proposed and its pending id so the user can confirm.\n"
    "- Pausing, resuming, or cancelling an existing job happens immediately, no confirmation "
    "needed.\n"
    "- In Phase 1, confirmed jobs are stored and scheduled but not yet actually executed on a "
    "background timer -- that ships in Phase 2. Be upfront about that if asked.\n"
)
