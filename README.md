---
title: Messa
emoji: 💬
colorFrom: purple
colorTo: blue
sdk: docker
app_port: 7860
---

# Messa -- multi-agent personal assistant (Phase 1 + Phase 2 + Phase 3)

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

### 2. Point DATABASE_URL at real Neon, run all six migrations

```
DATABASE_URL=postgresql+asyncpg://user:pass@ep-xxxx.neon.tech/dbname?sslmode=require
BROWSERBASE_API_KEY=...      # required -- see "Browserbase" below
# MESSA_LIVE_VIEW_BASE_URL=... # optional -- see "Phase 3: watch the browser live" below
```

(Verified asyncpg parses Neon's `sslmode=require` param with no extra code
-- it validates it as a real libpq option, doesn't just ignore it.)

```bash
psql "$DATABASE_URL" -f migrations/002_projects_and_channel.sql
psql "$DATABASE_URL" -f migrations/003_user_email.sql
psql "$DATABASE_URL" -f migrations/004_deepsearch_sessions.sql
psql "$DATABASE_URL" -f migrations/005_browserbase.sql
psql "$DATABASE_URL" -f migrations/006_live_view.sql
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
- **Every one of Messa's messages gets texted, not just the turn's last
  one.** Found via your own report that the live-view link never arrived:
  the pre-delegation acknowledgment ("Checking that now, watch it live
  here: <link>") was being generated and logged correctly, but the old
  code waited for the *entire* turn -- including a possibly multi-minute
  deepsearch run -- to finish, then texted only its final AI message.
  `cli.run_turn`/`run_message` now take an `on_ai_message`/`send` callback
  that fires immediately for each AI message as it's produced, so an
  acknowledgment goes out as its own SMS the moment Messa says it (and the
  typing indicator re-arms after it), with the eventual summary following
  as a second text once the delegation finishes -- matching how a person
  actually texts, and the reason live-view links (or any other "starting
  this now" message) actually reach your phone in time to be useful.
- **The live-view link is attached in code now, not written by the model.**
  The fix above still wasn't enough on its own -- a real run showed the
  model trailing off mid-sentence ("I'll look for gaming desks around $150
  -- checking a") right as it fired the deepsearch tool call, before ever
  reaching the link. That's not a streaming bug (`stream_mode="updates"`
  only ever hands back complete messages, never partial tokens) -- it was
  genuinely the entire, final `content` the model chose to generate before
  switching into tool-call mode. Trusting a model to reliably finish a
  sentence *and* reproduce a URL correctly, every time, right before a tool
  call, isn't reliable enough for something that should always be there.
  Messa is no longer asked to write the link into her own text at all;
  `run_turn` now reports whenever a message carries a deepsearch delegation
  (even one with no acknowledgment text at all), and `run_message` appends
  "Watch it live: `<link>`" to whatever she said -- or sends it as its own
  short message if she said nothing -- guaranteed correct and complete
  every time, independent of the model's phrasing.

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
BROWSERBASE_API_KEY       # required -- deepsearch has no local-browser fallback, see "Browserbase" below
MESSA_LIVE_VIEW_BASE_URL  # e.g. https://live.textmessa.com -- see "Phase 3: watch the browser live" below
SENDBLUE_API_KEY
SENDBLUE_API_SECRET
SENDBLUE_NUMBER
SENDBLUE_WEBHOOK_SECRET   # if you set one, see Phase 2 step 3 above
```

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

Watch the build in the Space's **Logs** tab. It's a much lighter build now
than earlier rounds -- no local Chromium download/install step, since
deepsearch drives a remote Browserbase browser instead (see "Browserbase"
below) -- so rebuilds are quick unless `requirements.txt` changed.

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
  you attach paid persistent storage. Concretely: local `sessions`/`outputs`
  files (CLI-only, not used by the server) don't survive a restart. Neon
  itself is unaffected -- it's an external DB, not Space storage, so users/
  tasks/reminders/deepsearch_sessions/message_history are all fine. Nor is
  deepsearch's login persistence affected anymore -- since the Browserbase
  switch (see below) that's a Browserbase Context, not a directory on this
  container's disk, so it survives restarts fine.

## Browserbase

Two rounds of testing hit the same wall from different angles: "Chrome
isn't installed in the sandbox." Both times the root cause I found and
fixed was real (MCP's stdio transport not passing through the parent
environment; then a `--browser` flag that silently pointed at a totally
different, uninstalled Chrome build) -- but you redeployed with both fixes
in and it was still down. Rather than keep chasing a third HF-container-
specific quirk, your call to switch to Browserbase was the right one: it
runs the actual Chromium on its own infrastructure, and our container just
drives it remotely over CDP (`@playwright/mcp@latest --cdp-endpoint
<url>`). deepsearch's own architecture -- the LangChain agent, the full
Playwright tool set, the domain allow-list, the destructive-action approval
gate, the stale-snapshot guard -- is all completely unchanged; only *where*
the browser physically runs is different. This also directly answers your
question about whether Browserbase would get in the way of either
requirement:

- **Keeping your own fine-tuned agent driving the browser**: yes, fully
  preserved. This deliberately does *not* use Browserbase's own
  Stagehand/agent product -- `--cdp-endpoint` just hands your existing
  `@playwright/mcp` tool-calling setup a remote browser to control, exactly
  matching Browserbase's own guidance for "let your own agent drive it,
  Browserbase is just the infrastructure layer" use cases.
- **Interactive live browser streaming (Phase 3)**: yes, this is what
  Browserbase's Live View is for. Every deepsearch run now captures a
  `debuggerFullscreenUrl` (an iframe-embeddable link to watch that exact
  session live) into `deepsearch_sessions.live_view_url` the moment it
  opens the browser -- see `migrations/005_browserbase.sql` and
  `messa/channels/browserbase.py`. This is now fully wired into a reply --
  see "Phase 3: watch the browser live" below.

What changed concretely:

- **Per-user login persistence moved from disk to Browserbase.** The old
  `--user-data-dir deepsearch_profiles/user-<id>` directory is gone;
  `users.browserbase_context_id` now holds one Browserbase Context per
  user (created once via `db.get_browserbase_context_id`/
  `save_browserbase_context_id`, reused on every later session). This is
  strictly better than the old approach -- Contexts live indefinitely on
  Browserbase's side, so they survive an HF Space restart, unlike the old
  local directory.
- **`BROWSERBASE_API_KEY` is now required** -- there's no local-Chromium
  fallback anymore, deepsearch simply doesn't work without it. Get a free
  key at browserbase.com; per their current docs no
  `BROWSERBASE_PROJECT_ID` is needed anywhere, the API key alone resolves
  the project.
- **The Dockerfile is lighter.** No more `playwright install --with-deps
  chromium` (a multi-minute build step) or `PLAYWRIGHT_BROWSERS_PATH`/
  `MESSA_DEEPSEARCH_HEADLESS` plumbing for a browser that no longer runs in
  this container. Node.js and the `@playwright/mcp` npx-cache warm-up are
  still there -- that package is the MCP *client*, which still runs
  locally and now just connects out to Browserbase over CDP instead of
  launching a local process.
- **Worth knowing about the free Browserbase plan** (per the setup doc you
  saved): no Proxies, no Verified/auto-CAPTCHA-solving. Only matters if
  deepsearch ever needs to get through a bot-protected or CAPTCHA-walled
  site -- ordinary browsing/research is unaffected. Their Model Gateway's
  $5 token cap is irrelevant here too, since Messa brings its own LLM via
  OpenRouter rather than using Browserbase's.
- Needs `migrations/005_browserbase.sql` (adds `users.browserbase_context_id`
  and `deepsearch_sessions.live_view_url`, both additive/no-op-safe like
  every other migration here).

## Phase 3: watch the browser live

A permanent, shareable link per user that shows Browserbase's live view
while deepsearch is browsing, and a calm "nothing happening right now"
state otherwise -- no login, just the link, meant to be opened straight
from the text Messa sends.

**How it works:** `users.live_share_token` is a random, unguessable token
generated once per user and reused forever (`db.get_or_create_live_share_token`).
The link is `<MESSA_LIVE_VIEW_BASE_URL>/live/<token>`. Separately,
`users.live_view_url`/`live_view_task`/`live_view_started_at` track whether
a browser is open *right now* -- set the instant `BrowserToolProvider`
opens a Browserbase session, cleared the instant that delegation is done
with it, success or failure alike (a `try`/`finally` in
`tools/deepsearch_tools.py`, so a crash mid-run can't leave the page stuck
showing "live" forever -- there's also a 15-minute staleness cutoff as a
second safety net for the one thing `finally` can't catch, the whole
process getting killed). This is deliberately separate from
`deepsearch_sessions`, which tracks completed/resumable run *history*, not
"is something on screen this second" -- a session can sit there marked
resumable for hours after its browser already closed.

The page itself (`messa/live_view_page.py`, served by `messa/server.py` at
`GET /live/<token>`) is a single self-contained HTML file with no build
step -- it polls its own `GET /live/<token>/status` JSON endpoint every 4
seconds and swaps between the idle state and Browserbase's iframe with no
manual refresh. An unknown token still gets a 200 with the page shell (so
a typo'd link doesn't just 404 blankly); the page's own JS is what shows
"this link isn't valid" once its first status poll comes back 404.

**Per your request**, Messa now includes this link in her acknowledgment
message *every single time* she delegates to deepsearch (not just the
first) -- e.g. "Checking that now, watch it live here: <link>" -- since
it's always the same permanent link for that user, repeating it costs
nothing and means you never have to go dig up an old text to find it.
The link itself is attached in code (`cli.py`'s `run_message`), not written
by the model -- see "Debugging the missing live link" below for why. It
only shows up in a message when `user.live_view_share_url` resolves to a
real URL, which needs both `LIVE_VIEW_BASE_URL` (now defaults to
`https://live.textmessa.com`, no secret required) and a per-user
`live_view_token` (needs migration 006 -- see below). Missing either one
means Messa simply never mentions a link that turn, so there's no risk of
texting a broken one.

**Connecting textmessa.com:** Hugging Face Spaces supports custom domains,
but only as a subdomain (not the bare apex) via a CNAME, and only on a
**PRO** (or Team/Enterprise) account -- separate from the Space's own
free/paid hardware tier. The Space also needs to be public or "protected,"
not fully private, for a custom domain to attach at all.

1. In your DNS provider, add `live` (or whatever subdomain you prefer) as
   a **CNAME** pointing to `hf.space`. Don't leave any other record type
   (A/AAAA) at that same label -- that's the most common reason a custom
   domain gets stuck in "pending."
2. In the Space's **Settings -> Custom Domain**, add `live.textmessa.com`.
3. Wait for it to flip from "pending" to "ready" -- HF issues the TLS
   certificate automatically once DNS resolves correctly, no cert work on
   your end.
4. Set `MESSA_LIVE_VIEW_BASE_URL=https://live.textmessa.com` as a Space
   secret/variable (see "Add your secrets" above) and redeploy.

Until that's done, you can still use the whole feature today with
`MESSA_LIVE_VIEW_BASE_URL` set to your Space's own
`https://<you>-<space>.hf.space` URL -- the page and the link both work
identically either way, `live.textmessa.com` is just a nicer hostname on
top of the same backend.

Needs `migrations/006_live_view.sql` (adds `users.live_share_token`,
`live_view_url`, `live_view_task`, `live_view_started_at` -- additive/
no-op-safe like every other migration here; without it, deepsearch still
works exactly as before, Messa just never has a link to mention).

### Two reliability bugs this feature's testing surfaced (fixed, affect every migration)

Rolling out migration 006 against your live Neon DB while the Space kept
running exposed two real bugs in `db.py` -- not specific to this feature,
they'd have bitten *any* future migration the same way:

- **A stale-schema cache that never healed.** `_has_column`/`_has_table`
  (what every additive migration's "has this been applied yet?" check
  goes through) cached whatever answer they got *first* -- including
  "column doesn't exist" -- in a plain in-memory dict with no expiry. If
  your Space's process made its first check before you'd run that turn's
  migration against Neon (very possible, since the two happen as separate
  steps), it would keep insisting the column was missing forever, even
  seconds after the migration actually succeeded -- only a full restart
  would make it notice. This is exactly why your live-view link never
  generated and why a token that genuinely existed in `users` still 404'd
  at `/status`. Fixed: `True` is still cached forever (a column that
  exists can't stop existing), but `False` is never cached -- it's
  cheap to re-check, and it self-heals the instant the migration runs, no
  restart required.
- **"cached statement plan is invalid due to a database schema or
  configuration change"** -- the turn-failure you saw in the logs. Your
  `DATABASE_URL` points at Neon's pooled endpoint (the `-pooler` host),
  which is PgBouncer in transaction-pooling mode: a single asyncpg
  connection can get routed to a different physical Postgres backend
  between queries, and asyncpg's default server-side statement caching
  doesn't survive that combined with live DDL (exactly what running a
  migration against the same DB a running process is using does). This is
  a known asyncpg+PgBouncer incompatibility -- Neon's own docs recommend
  the same fix Neon suggests: `get_pool()` now passes
  `statement_cache_size=0` to `asyncpg.create_pool`.

If you're still seeing either symptom after pulling this update, a Space
restart clears both a stale connection pool and this process's in-memory
cache in one shot -- but going forward neither should require one.

### Debugging the missing live link (this round)

You reported deepsearch and the live-view page both working end to end,
but the link itself still never arriving over SMS, and gave three specific
hypotheses for where the chain breaks. Each was checked directly rather
than guessed at:

1. **"Tool call name detection gap"** (a `deepsearch`-named tool call
   bypassing the `"task"`-name check `run_turn` looks for) -- ruled out by
   reading the installed `deepagents` library's own source
   (`middleware/subagents.py`'s `_build_task_tool`). Every subagent this
   project defines (plain dict or `CompiledSubAgent`, which deepsearch is)
   is invoked through one single, unconditionally-named `"task"` tool --
   there's no code path in this version of deepagents that would ever
   expose deepsearch as its own directly-named tool. This hypothesis isn't
   possible given how `registry.py` builds the orchestrator.
2. **The model going straight to the tool call with no acknowledgment
   text** -- and the related concern that LangGraph might split a single
   completion's text and tool call across separate stream events, which
   would defeat `delegating_to` detection either way. Rather than reason
   about this, I built a real integration test: a fake chat model wired
   into the *actual* `create_deep_agent`/`build_orchestrator` (only the
   model and Browserbase's session are faked; everything else -- the `task`
   tool, LangGraph's `astream(stream_mode="updates")`, `cli.run_turn`,
   `cli.run_message` -- is the real code), covering both a completion with
   text+tool_call together and one with an empty acknowledgment and only a
   tool_call. Both cases correctly produced a text with the live link
   attached. So neither of these is happening in this codebase as it
   stands.
3. **`MESSA_LIVE_VIEW_BASE_URL` missing/misconfigured on the Space** --
   this is the one hypothesis that *is* still a real possibility, and the
   one I can't check from outside your deployed Space or your live Neon
   DB. It's now partly moot (`LIVE_VIEW_BASE_URL` defaults to
   `https://live.textmessa.com` even with no secret set at all), which
   leaves the other half of `live_view_share_url`: a per-user
   `live_view_token`, which is `None` until migration 006 has actually run
   against the DB your Space is pointed at.

Since (1) and (2) are now confirmed not to be the cause and (3) can't be
checked from here, `run_message`'s `_on_ai_message` (in `cli.py`) now logs
a clear, specific line the moment a deepsearch delegation has no link to
attach, telling you exactly which half is missing:

```
Deepsearch delegation for user #<id> has no live-view link to attach --
live_view_token=None, LIVE_VIEW_BASE_URL='https://live.textmessa.com'.
live_view_token is None: migration 006_live_view.sql likely hasn't run
against this DB yet (or get_or_create_live_share_token failed).
```

Previously this was a silent no-op -- exactly why this bug took several
rounds just to localize. If the link is still missing on your next
deepsearch delegation, check the Space's logs for this line: it tells you
directly whether it's the token or the base URL, instead of another guess.
If it says `live_view_token is None`, the fix is confirming migration 006
has actually run against the same `DATABASE_URL` the deployed Space uses
(not just your local `.env`'s database).

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
  connects `@playwright/mcp` fresh to a new browser session on each
  delegation and fully closes/releases it once the subagent's task
  finishes -- so an idle session doesn't hold a browser open. Logins still
  persist between separate tasks because each user gets their own
  Browserbase Context rather than an unpersisted one-off session (see the
  "Browserbase" section above for why this replaced an earlier on-disk
  `--user-data-dir` approach).

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
BROWSERBASE_API_KEY=...      # required -- see "Browserbase" above
# MESSA_LIVE_VIEW_BASE_URL=... # optional -- see "Phase 3: watch the browser live" above
# optional, for the email agent once you're ready:
# COMPOSIO_API_KEY=...
# COMPOSIO_EMAIL_CONNECTED_ACCOUNT_ID=...
```

Run the additive migrations once (adds a lightweight `projects` table used
to group related requests, a `channel` column on `message_history` for
Phase 2's multiple channels, a `users.email` column for onboarding, a
`deepsearch_sessions` table for resumable research, the Browserbase
columns, and the live-view-link columns -- see "What I added beyond your
schema" below). Paste each file into the Neon SQL editor, or:

```bash
psql "$DATABASE_URL" -f migrations/002_projects_and_channel.sql
psql "$DATABASE_URL" -f migrations/003_user_email.sql
psql "$DATABASE_URL" -f migrations/004_deepsearch_sessions.sql
psql "$DATABASE_URL" -f migrations/005_browserbase.sql
psql "$DATABASE_URL" -f migrations/006_live_view.sql
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

There was also nowhere to store the per-user Browserbase Context id or a
session's live-view link once deepsearch moved off local Chromium (see
"Browserbase" above), so `migrations/005_browserbase.sql` adds
`users.browserbase_context_id` and `deepsearch_sessions.live_view_url`.

Finally, sharing a live view needed a permanent per-user link and a
"is a browser open right now" flag distinct from `deepsearch_sessions`'
longer-lived history (see "Phase 3: watch the browser live" above), so
`migrations/006_live_view.sql` adds `users.live_share_token`,
`live_view_url`, `live_view_task`, and `live_view_started_at`.

All six are additive/reversible and the app runs fine without them
(project tracking, email-saving, and session resumption just no-op;
without migration 005, deepsearch still runs against Browserbase, it just
can't remember your login between separate sessions or capture a live-view
link; without migration 006, deepsearch works exactly the same, Messa just
never has a link to mention).

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
