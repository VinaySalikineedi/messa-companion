---
title: Messa
emoji: 💬
colorFrom: purple
colorTo: blue
sdk: docker
app_port: 7860
---

# Messa -- multi-agent personal assistant (Phase 1 + Phase 2)

Messa is the orchestrator. She talks to you and delegates to five
specialist subagents through deepagents' built-in `task` tool:

- **deepsearch** -- live web browsing (Playwright MCP), ported from the
  original `agent2.py` with the same safety rails (domain allow-list,
  destructive-action confirmation, snapshot-freshness checks). Progress is
  saved per session so a run that hits its step limit can be resumed
  instead of restarting -- see "Changes from your second round of testing"
  below.
- **executive_assistant** -- tasks, reminders, notes, contacts, calendar.
- **email_agent** -- the user's own inbox via Composio (not yet configured
  -- see below).
- **document_agent** -- generates PDFs (reportlab).
- **routines_agent** -- recurring automations (cron jobs).

## Phase 2: texting Messa over Sendblue

An always-on webhook server (`messa/server.py`) so you can text Messa
directly -- iMessage/SMS/RCS via Sendblue, which auto-falls-back through
those in that order.

### 1. Get your Sendblue credentials

```bash
npm install -g @sendblue/cli
sendblue login              # OTP goes to vinaysalikineedi@gmail.com
sendblue show-keys          # -> apiKey, apiSecret
sendblue lines              # -> your assigned number
```

Add to `.env`:

```
SENDBLUE_API_KEY=...
SENDBLUE_API_SECRET=...
SENDBLUE_NUMBER=...           # from `sendblue lines`
# SENDBLUE_WEBHOOK_SECRET=... # optional, see step 3
```

### 2. Point DATABASE_URL at real Neon, run all four migrations

```
DATABASE_URL=postgresql+asyncpg://user:pass@ep-xxxx.neon.tech/dbname?sslmode=require
```

(Verified asyncpg parses Neon's `sslmode=require` param with no extra code
-- it validates it as a real libpq option, doesn't just ignore it.)

```bash
psql "$DATABASE_URL" -f migrations/002_projects_and_channel.sql
psql "$DATABASE_URL" -f migrations/003_user_email.sql
psql "$DATABASE_URL" -f migrations/004_deepsearch_sessions.sql
```

### 3. Run the server and register the webhook

```bash
uvicorn messa.server:app --host 0.0.0.0 --port 8000
```

Sendblue needs a public HTTPS URL to reach that -- for local dev, tunnel it
(e.g. `ngrok http 8000`), then:

```bash
sendblue webhooks set-receive https://<your-tunnel>.ngrok.app/webhook/sendblue
```

If you set a secret on that webhook (dashboard, or the CLI's webhook config),
set the matching `SENDBLUE_WEBHOOK_SECRET` in `.env` and the server verifies
every inbound request's `sb-signing-secret` header against it. Leave it
unset and no verification happens -- fine while the tunnel URL is private/
throwaway, worth turning on once it's a stable public endpoint.

### 4. Free tier heads-up

Your account is on Sendblue's Free Tier: a shared (not dedicated) number, up
to 10 verified contacts, and **inbound + replies only -- a contact has to
text your Sendblue number first** before Messa can reply to them (no cold
outbound). That's exactly the shape of "you text Messa, Messa replies," so
it's fine for testing this loop; it just means you can't yet use this for
Messa to proactively text you first (see "reminders/cron still don't
actually fire" below).

```bash
sendblue add-contact +1YOURNUMBER   # verify yourself as a contact first
```

### 5. What changed for this channel specifically

- **No markdown in replies.** `agents/registry.py`'s system prompt now
  checks `user.channel`: over `sms`/`imessage`/`rcs` it explicitly tells
  Messa to write plain text (no `**bold**`, bullets, headers, code fences)
  since iMessage/SMS don't render markdown. CLI replies are unaffected.
- **Typing indicator.** The server sends Sendblue's "..." bubble
  (`send-typing-indicator`) as soon as a message comes in, best-effort --
  a real UX answer (for this channel) to round 1's "slowness between
  steps" feedback, on top of Messa's existing "checking flights now..."
  acknowledgment line.
- **Fast ack, slow work.** Sendblue times out and retries a webhook after
  ~45s; a deepsearch-involving turn can easily take longer than that. The
  server acks with 200 immediately and does the actual Messa turn + reply
  in a background task, so Sendblue never sees a timeout/retry for a
  normal (if slow) conversation.
- **Destructive actions default to declined.** `approval.py`'s
  `CLIApprovalGate` blocks on `input()`, which would hang forever with no
  terminal attached to a webhook request. The server uses a new
  `DenyApprovalGate` instead -- clicks/typing/sends inside deepsearch or
  email_agent get a clean "blocked" result back rather than hanging.
  Non-destructive delegations (most of what those two do) are unaffected.
  Set `MESSA_SMS_AUTO_APPROVE_DESTRUCTIVE=true` to auto-approve instead,
  once you're comfortable with that tradeoff for your own account -- a
  real "pause mid-run, resume on your next text" approval flow (mirroring
  how deepsearch sessions resume) is a bigger feature I didn't build yet.
- **Users are created/resolved by phone number.** Same `save_profile_info`
  onboarding flow as the CLI, just triggered by whoever's texting in
  instead of `MESSA_CLI_USER_PHONE`.

### Known Phase 2 limitation

Reminders/cron jobs still just log "this would fire" (see
`messa/background.py`, unchanged this round) rather than proactively
texting you -- wiring that up needs a decision on how it interacts with
Free Tier's reply-window rules (a proactive text is arguably not a "reply,"
and the docs don't fully spell out how that's enforced below the paid AI
Agent/Blue Ocean plans), so I left it as a deliberate follow-up rather than
guessing at the policy. Happy to build it once you've either upgraded or
confirmed how you want to handle that.

### Testing notes

I can't reach OpenRouter or Sendblue's API from my sandbox, so I tested
everything else against real local Postgres with `build_orchestrator` and
the Sendblue calls mocked: signature verification (good/bad secret),
outbound-echo and empty-content filtering, user resolution/creation by
phone number (same number -> same user across calls), `message_history`
rows getting the right `channel` value, the Deny-vs-AutoApprove gate
selection, and that an exception mid-turn sends a fallback reply instead of
silently dropping the message. The actual webhook round-trip with Sendblue
and a live model needs your own end-to-end test.

## Deploying to Hugging Face Spaces

Borrowing a piece of Phase 4 early: a Space gives Sendblue a stable public
URL, so you don't need a local tunnel (ngrok etc.) at all for testing. This
covers the webhook server -- Phase 3's live browser-streaming link and the
rest of Phase 4's checklist are still separate, later work.

Your existing Space was created with the Gradio SDK, which runs a specific
UI framework -- not a fit for a raw FastAPI backend like `messa/server.py`.
Good news: a Space's SDK is just the `sdk:` field in `README.md`'s
frontmatter (I added one at the very top of this file, set to `docker`, and
the app runs as its own container: instead of an `app.py` Gradio expects,
Docker Spaces run the `Dockerfile` at your repo root, which I've also added).
Pushing that frontmatter change plus the `Dockerfile` converts the same
Space to Docker -- no need to create a new one.

### 1. Confirm/set the Space private

Visibility isn't a frontmatter field -- check it under your Space's
**Settings -> Change visibility**. Worth doing before anything else, since
this is a personal assistant with access to your email/tasks/calendar.

### 2. Add your secrets

Space **Settings -> Variables and secrets** -> add each of these as a
**secret** (not a public variable) -- HF injects them as real environment
variables into the running container, so nothing in `config.py` needs to
change:

```
OPENROUTER_API_KEY
DATABASE_URL              # your real Neon connection string
SENDBLUE_API_KEY
SENDBLUE_API_SECRET
SENDBLUE_NUMBER
SENDBLUE_WEBHOOK_SECRET   # if you set one, see Phase 2 step 3 above
```

`MESSA_DEEPSEARCH_HEADLESS=true` is already baked into the Dockerfile as a
build-time default (there's no display on a Space), so you don't need to
set it yourself unless you want to override something.

### 3. Push

From your local `textMessa` folder (already connected to this session, but
this push itself has to run in your own terminal -- I can't reach
huggingface.co from mine):

```bash
git remote add space https://huggingface.co/spaces/<your-username>/<your-space-name>
git add -A
git commit -m "Convert to Docker SDK: Messa webhook server"
git push space main
```

(Use `--force` only if your Space's git history genuinely conflicts with
this folder's -- e.g. it still has Gradio's starter commit -- and you're
sure you want this folder's content to win.)

Watch the build in the Space's **Logs** tab. The Playwright/Chromium install
step adds a few minutes the first time; after that, Docker layer caching
makes rebuilds faster unless `requirements.txt` changed.

### 4. Point Sendblue at it

Once it's live, your Space's URL is `https://<your-username>-<your-space-name>.hf.space`
(also shown at the top of the Space page). Register it:

```bash
sendblue webhooks set-receive https://<your-username>-<your-space-name>.hf.space/webhook/sendblue
```

### Two things worth knowing about the free CPU Basic tier

- **It sleeps after ~48h idle**, and wakes on the next request -- so the
  first text after a long gap may be slow enough to eat into Sendblue's
  ~45s webhook timeout on that one message (it'll likely still arrive via
  Sendblue's retry; every message after that is fast, since the container's
  already warm).
- **The filesystem is ephemeral** -- wiped on every restart/redeploy unless
  you attach paid persistent storage. Concretely: deepsearch's per-user
  Chromium profile directories (the login persistence from round 1's
  feedback) reset to logged-out on a restart, and local `sessions`/`outputs`
  files (CLI-only, not used by the server) don't survive one either. Neon
  itself is unaffected -- it's an external DB, not Space storage, so users/
  tasks/reminders/deepsearch_sessions/message_history are all fine. If
  losing deepsearch logins on restart becomes a real annoyance, the fix is
  storing Playwright's exported `storage_state` in Postgres instead of on
  disk (same pattern as deepsearch_sessions) and rehydrating it per
  delegation -- happy to build that if/when it matters to you.

## Changes from your second round of testing

- **Renamed browser_agent -> deepsearch.** Same subagent, new name
  everywhere (config, tracing colors, system prompts, files). If you have
  the old build checked out, delete `messa/tools/browser_tools.py` -- it's
  replaced by `messa/tools/deepsearch_tools.py`.
- **Resumable sessions.** Previously, when deepsearch hit its step limit,
  Messa correctly retried without asking you -- but the subagent restarted
  from nothing and redid finished work. Now every deepsearch run persists
  its full message history (including tool calls/results) to a new
  `deepsearch_sessions` table after every delegation, whether it finished
  or got cut off. When it's cut off, Messa's reply names the session id;
  telling her "continue session #12" (or anything with "session #12" in
  it) makes her delegate again with that reference, and deepsearch replays
  the saved history before continuing instead of starting over. She also
  has a new `list_deepsearch_sessions` tool to check what's in progress or
  already found. Needs `migrations/004_deepsearch_sessions.sql`.
- **Step budget separated from the orchestrator's own limit.** A new
  `MESSA_DEEPSEARCH_MAX_STEPS` (default 40) governs how many steps a single
  deepsearch delegation gets before it saves progress and stops -- deep
  research legitimately needs more room than Messa's own reasoning loop, and
  hitting this cap is an expected, handled case (session saved + resumable),
  not an error.

## Changes from your first round of testing

- **Speed.** `create_deep_agent`'s default harness silently gives every
  agent a virtual filesystem (`ls`/`read_file`/`write_file`/`edit_file`/
  `delete`/`glob`/`grep`), a shell `execute` tool, and an auto-added
  "general-purpose" subagent -- none of which this project uses, and all of
  which were extra tool-schema tokens on every single model call across all
  6 agents. `agents/registry.py` now registers a `HarnessProfile` once at
  import time that strips all of that (confirmed via deepagents' source --
  it filters `request.tools` right before the model sees them -- and via
  the `task` tool's own description no longer listing "general-purpose").
  Messa's system prompt also now explicitly tells her to send a short
  acknowledgment ("Checking flights now...") before delegating, instead of
  going straight to a silent tool call, so multi-step requests don't read
  as dead air. The inherent cost of a multi-hop architecture (Messa's own
  turn -> delegate -> subagent's steps -> summarize) is still there --
  that's real work, not overhead -- but this cuts the token/latency tax
  around it. Phase 2 will need this same "send interim updates" idea
  applied to actual outbound texts, not just console prints.
- **Onboarding.** New users now go through name -> email (skippable) ->
  location, driven by `users.onboarding_step` -- a column your schema
  already had for exactly this. `save_profile_info` (a tool on Messa
  herself) writes each answer and advances the step; Messa's system prompt
  is rebuilt every turn (see `cli.py`'s `_load_user_context`) so it always
  reflects the latest onboarding state and known profile fields. Needs
  `migrations/003_user_email.sql` (adds `users.email`, which wasn't in the
  original schema).
- **UTC.** Confirmed, no code change needed: every due-date/reminder/cron
  comparison already uses `datetime.now(timezone.utc)` explicitly rather
  than naive local time, so this is correct regardless of what timezone the
  host machine/container is set to.
- **Browser lifecycle.** deepsearch (then still called browser_agent) is no
  longer a long-lived Playwright session held open for the whole CLI
  process. It's a `CompiledSubAgent` (see `tools/deepsearch_tools.py`) that
  launches `@playwright/mcp` fresh on each delegation and fully closes that
  subprocess (killing the browser) once the subagent's task finishes -- so
  an idle session doesn't hold a Chromium process open. Logins still
  persist between separate tasks because each user gets their own on-disk
  profile directory (`--user-data-dir deepsearch_profiles/user-<id>`)
  rather than an in-memory one.

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

Run the additive migrations once (adds a lightweight `projects` table used
to group related requests, a `channel` column on `message_history` for
Phase 2's multiple channels, a `users.email` column for onboarding, and a
`deepsearch_sessions` table for resumable research -- see "What I added
beyond your schema" below). Paste each file into the Neon SQL editor, or:

```bash
psql "$DATABASE_URL" -f migrations/002_projects_and_channel.sql
psql "$DATABASE_URL" -f migrations/003_user_email.sql
psql "$DATABASE_URL" -f migrations/004_deepsearch_sessions.sql
```

Everything else works without them -- `db.py` checks for the table/column
first and no-ops (or just skips storing email) if they're missing.

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
deepsearch: ...`. See `messa/console.py`'s docstring for why this is
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
SMS/email (a channel with no synchronous stdin), unlike deepsearch's
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

It also had `users.onboarding_step` (used as designed, see above) but no
`users.email` to put an onboarding-collected email into, so
`migrations/003_user_email.sql` adds that column.

There was also nowhere to persist a deepsearch run's progress, so
`migrations/004_deepsearch_sessions.sql` adds `deepsearch_sessions`
(`user_id`, `title`, `status`, `messages` -- the full serialized message
history, `summary`, `steps_used`, timestamps). `messages`/`summary` are
`TEXT`, following the same "JSON as a string column" convention your
schema already uses for `pending_actions.payload`/`audit_logs.payload`,
rather than introducing JSONB as a second convention.

All four are additive/reversible and the app runs fine without them
(project tracking, email-saving, and session resumption just no-op --
deepsearch still works, it just can't resume a cut-off run).

## Known Phase 1 limitations (by design -- later phases cover these)

- Single hardcoded CLI user (`MESSA_CLI_USER_PHONE` in `.env`, defaults to
  a placeholder) -- goes through onboarding once, like any new user, then
  stays "known" for future CLI runs. Phase 2 resolves the user from the
  inbound channel instead of one fixed phone number.
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
