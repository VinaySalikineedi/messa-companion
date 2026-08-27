"""Local background pollers for the CLI -- a console-only PREVIEW, not real
delivery. For actual production delivery (real SMS/iMessage via Sendblue,
and real re-invocation of Messa for due cron jobs), see
server.py's `_production_reminder_loop` / `_production_cron_loop`, started
from that app's startup event. This module is only ever started from
cli.main_async, and is intentionally never wired into server.py -- the CLI
has no Sendblue number to send to, and a console print is exactly the
right amount of feedback for local interactive testing.

Two asyncio tasks run alongside the REPL:
  * due reminders -> printed as a proactive Messa line, marked sent.
  * due cron jobs -> printed as "this would fire now", rescheduled to their
    next occurrence. Not actually re-invoking the agent here -- that's what
    server.py's production loop does for real, over a real channel.

Both are best-effort: any DB/other error is logged and the loop keeps going
rather than taking down the CLI.
"""
from __future__ import annotations

import asyncio

from . import console, db
from .tools.routines_tools import compute_next_run

POLL_INTERVAL_SECONDS = 30


async def _reminder_loop() -> None:
    while True:
        try:
            due = await db.get_due_reminders()
            for r in due:
                console.proactive(f"Reminder: {r['message']}")
                await db.mark_reminder_sent(r["id"])
        except Exception as e:  # noqa: BLE001
            console.system(f"[reminder poller error] {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _cron_loop() -> None:
    while True:
        try:
            due = await db.get_due_cron_jobs()
            for job in due:
                console.proactive(
                    f"[routine due] '{job['prompt_or_task']}' would run now "
                    f"(Phase 2 will actually execute it)."
                )
                next_run = compute_next_run(job["cron_expression"], job["user_timezone"])
                await db.reschedule_cron_job(job["id"], next_run)
        except Exception as e:  # noqa: BLE001
            console.system(f"[cron poller error] {e}")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


def start_background_pollers() -> list[asyncio.Task]:
    return [asyncio.create_task(_reminder_loop()), asyncio.create_task(_cron_loop())]
