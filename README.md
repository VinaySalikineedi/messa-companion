# Messa -- multi-agent personal assistant (Phase 1)

Messa is the orchestrator. She talks to you and delegates to five
specialist subagents through deepagents' built-in `task` tool:

- **browser_agent** -- live web browsing (Playwright MCP), ported from the
  original `agent2.py` with the same safety rails (domain allow-list,
  destructive-action confirmation, snapshot-freshness checks).
- **executive_assistant** -- tasks, reminders, notes, contacts, calendar.
- **email_agent** -- the user's own inbox via Composio (not yet configured
  -- see below).
- **document_agent** -- generates PDFs (reportlab).
- **routines_agent** -- recurring automations (cron jobs).

## Setup

```bash
python3 -m venv venv   # you already have this
source venv/bin/activate
pip install -r requirements.txt
```

`.env` needs:

```
OPENROUTER_API_KEY=...       # already set
DATABASE_URL=...             # already set (Neon)
# optional, for the email agent once you're ready:
# COMPOSIO_API_KEY=...
# COMPOSIO_EMAIL_CONNECTED_ACCOUNT_ID=...
```

Run the additive migration once (adds a lightweight `projects` table used
to group related requests, and a `channel` column on `message_history` for
Phase 2's multiple channels -- see "What I added beyond your schema"
below). Paste `migrations/002_projects_and_channel.sql` into the Neon SQL
editor, or:

```bash
psql "$DATABASE_URL" -f migrations/002_projects_and_channel.sql
```

Everything else works without it -- `db.py` checks for the table first and
no-ops if it's missing.

## Run

```bash
python -m messa.cli                  # new session
python -m messa.cli --session foo    # start/resume a named session
python -m messa.cli --list-sessions  # list saved local sessions
```

Session transcripts are stored locally as `sessions/<id>.jsonl`. This is
separate from `message_history` in the DB, which is per-*user* (not
per-CLI-run) and is what Phase 2's SMS/email channels will read/write too.

## How tracing works

Every tool call -- Messa's own and every subagent's -- prints in real time
as it executes: `[label] calling tool(args)` then `[label] <- returned:
...`. Messa's delegation to a subagent prints as `messa -> delegating to
browser_agent: ...`. See `messa/console.py`'s docstring for why this is
done by instrumenting each tool directly rather than trying to parse
LangGraph's nested subgraph stream events.

## The confirm-before-write pattern

Your schema's `action_type` enum (`create_task`, `update_task`,
`delete_task`, `create_reminder`, `cancel_reminder`,
`create_calendar_event`, `update_calendar_event`, `delete_calendar_event`,
`delete_note`, `create_recurring_cron`) is exactly the set of mutations
that go through `pending_actions` instead of writing directly:

1. `executive_assistant` / `routines_agent` call a `propose_*` tool ->
   inserts into `pending_actions`, nothing changes yet.
2. Messa relays the proposal to you in the chat and waits for an explicit
   yes.
3. You confirm -> Messa (only Messa -- subagents can't do this
   themselves) calls `confirm_pending_action`, which applies the change
   and writes an `audit_logs` row. A no calls `reject_pending_action`.

Everything not in that enum (reads, creating a note, contacts) writes
directly -- no round trip.

This is the same mechanism that will let Phase 2 confirm actions over
SMS/email (a channel with no synchronous stdin), unlike the browser agent's
destructive-action confirmation, which still blocks on `input()` in Phase 1
(see `messa/approval.py` -- it's a pluggable interface for exactly this
reason).

## What I added beyond your schema

Your `neon-schema.sql` didn't have anywhere to track "a request as a
project," so `migrations/002_projects_and_channel.sql` adds:

- `projects` table (+ `tasks.project_id`) -- Messa calls `track_project`
  when a request looks multi-step.
- `message_history.channel` -- so Phase 2 can tell CLI/SMS/email messages
  apart in the same table.

Both are additive/reversible and the app runs fine without them (project
tracking just no-ops).

## Known Phase 1 limitations (by design -- later phases cover these)

- Single hardcoded CLI user (`MESSA_CLI_USER_PHONE` in `.env`, defaults to
  a placeholder). Phase 2 resolves the user from the inbound channel
  instead.
- Routines agent's cron jobs are created/scheduled but not actually
  executed on a timer yet -- `messa/background.py` polls and prints what
  *would* fire, proving the DB/scheduling math, but re-invoking Messa on
  schedule needs Phase 2's always-on backend process.
- Email agent's Composio integration is written against Composio's
  documented client API but **not tested against a live account** -- I
  didn't have a Composio API key while building this. Verify the action
  slugs in `messa/tools/email_tools.py` against
  https://docs.composio.dev once you add `COMPOSIO_API_KEY` and connect
  Gmail.
- No WhatsApp (per your call -- Sendblue doesn't support it; dropped for
  now, add a provider like Meta's WhatsApp Cloud API later if you want it
  back).
