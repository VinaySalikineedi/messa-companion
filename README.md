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

### The live page now shows what deepsearch is doing, not just the video

**Per your request**, the live-view page is no longer just the raw
Browserbase iframe. The video now sits inside one lifted card, with a
one-line description of *what deepsearch is doing right now* above it
(e.g. "Navigating to https://www.amazon.com/s?k=gaming+desk") and a
scrolling chain-of-thought log below it, listing every action taken so
far this task. Both are specific to the task currently running -- they
reset to nothing the moment a new deepsearch delegation starts, and
disappear (the page falls back to the idle state) the instant the browser
closes, success or failure alike.

This is genuinely live, not simulated: `messa/live_activity.py` is a
small in-memory, per-user log that `tools/deepsearch_tools.py`'s guarded
tool-call wrapper writes to on every single browser action (navigate,
click, type, snapshot, etc. -- see `_describe_action` for how a raw tool
call like `browser_navigate(url=...)` becomes "Navigating to ..."), and
`server.py`'s `/live/<token>/status` route reads it back on every poll.
It's deliberately not persisted to Postgres -- it only matters for the
lifetime of one in-flight task, and already has the same natural
clear-on-close point (a `try`/`finally` in `deepsearch_tools.py`) as
`live_browser_active` does.

**Two visual skins, your choice:**

- `"polished"` -- soft gradient background, one lifted rounded card,
  description and chain-of-thought both feel like part of a normal
  consumer product.
- `"terminal"` -- black background, monospace, a fake terminal titlebar,
  `$ `-prefixed lines, a blinking cursor on the newest log line. Same
  information, gritty/dev-tool mood.

`live_view_page.py`'s `LIVE_VIEW_STYLE` constant at the top of the file
is **the variable you can change** -- flip it between `"polished"` and
`"terminal"` and redeploy to switch the default everyone sees. For
comparing them side by side without redeploying at all, append
`?style=polished` or `?style=terminal` to any live link -- that overrides
the default for just that one page load and touches nothing stored.

While building this I also found and fixed a real, pre-existing bug in
the page's own polling JS: `showIdle()` only replaced the "Loading..."
placeholder if a live URL had been shown *before* (`if (currentUrl !==
null)`), which meant a link that had never once been active -- brand new,
or opened before your first deepsearch run -- would sit on "Loading..."
forever, since idle and "haven't rendered anything yet" both looked like
`currentUrl === null` to that check. Fixed by tracking what's actually
rendered (`shownState`: `"idle"` / `"invalid"` / `"active:<url>"`) instead
of inferring it from the iframe URL alone. Verified with Playwright
screenshots of a fresh page load against a canned idle response, in both
skins, before and after the fix.

### Five UI fixes to the live page (dead space, sizing, live indicator, closing screen)

**Per your screenshots and request**, five changes to `live_view_page.py`
(plus small supporting changes elsewhere):

**1 & 2 -- the dead space below the video, and the card growing downward.**
Both came from the same root cause: `.video-wrap` used to be
`flex: 1 1 auto`, stretching to fill whatever space was left in the card
regardless of the actual browser content's shape. Browserbase's embedded
live-view page preserves its own aspect ratio and letterboxes rather than
distorting to fill an arbitrarily-shaped container -- so a tall, oddly-
shaped `.video-wrap` showed the real video plus a big blank strip below
it. Two things needed fixing at once: the video needed to be exactly the
right shape, and the card as a whole needed a height that never grows as
`.log` (the chain-of-thought panel) accumulates lines.

I pinned `channels/browserbase.py`'s `create_session` to a known,
explicit `browserSettings.viewport` (1280x800 -- Browserbase's docs don't
actually commit to a default, so rather than guess I gave it one), and
added a `fitVideo()` function to the page's JS that measures the real
space left in the card (its height, minus the description row, minus a
guaranteed minimum for the log) and sets `.video-wrap`'s height in pixels
to the largest box matching that 1280:800 ratio that still fits -- full
card width whenever there's room (this is also what makes the browsing
window itself noticeably bigger; I also raised the card's own
`max-width` from 960px to 1320px and cut its outer padding), a bit
shorter only on a short window, so the log never gets squeezed away
entirely. `.log` changed from a `max-height: 34%` block that hid itself
via `display: none` when empty (which just relocated the dead space
rather than fixing it) to `flex: 1 1 auto` with real internal scrolling,
so new lines scroll *inside* the card's fixed height instead of pushing
it taller.

While testing this at different window sizes I also found a genuine,
separate flexbox bug: `main` (the row containing the card) didn't have
`min-height: 0`, so flexbox refused to shrink it below its content's
natural minimum size -- meaning a tall card could push the *entire
document* taller than the browser's viewport instead of being contained,
which is exactly the "bounding box extends downward" failure mode you
flagged. Fixed by adding `min-height: 0` to `main`, `.stage`, and the
card itself. Verified with Playwright screenshots across five window
sizes (a normal 1440x900 desktop, a wide 1800x1100, a short 1440x680, and
a narrow 390x844 mobile portrait -- the shape an iMessage in-app browser
actually opens at) and confirmed via `document.documentElement.scrollHeight`
that the page's total height exactly matches the viewport in every case,
never taller.

**3 -- the live indicator.** Replaced the single pulsing dot with a small
solid core plus two staggered, outward-fading rings (a "radar ping," each
ring delayed half a cycle behind the other so the effect reads as
continuous rather than a single dot flashing on a timer), plus a "LIVE"
text label that fades in next to it. Same treatment in both skins
(square corners in "terminal," round in "polished"), same red -- the ask
was for it to look higher-quality, not to change what it signals.

**4 -- a visible cursor showing where deepsearch clicks.** I looked into
this and didn't implement it, and want to be upfront about why rather
than quietly skipping it. The live-view `<iframe>` embeds Browserbase's
own hosted debug page, which is cross-origin -- this page's own JS can't
reach inside it to draw an overlay. The technically real way to get a
visible custom cursor would be to inject a small script into the actual
pages deepsearch visits (via a second, independent CDP connection to the
same Browserbase session, listening for the real `mousemove` events
Playwright's clicks already generate, and drawing a synthetic cursor
element that follows them) -- Chrome does support more than one CDP
client attached to the same browser, so this isn't impossible. But it
means a second client attaching to the exact browser session
`@playwright/mcp` already owns and is actively driving for the real
task, and I have no real Browserbase credentials in this sandbox to test
whether that's actually safe or whether it interferes with deepsearch's
own navigation/snapshot state. Given that deepsearch's actual ability to
browse correctly is the thing that matters most here, I'd rather flag
that risk and get your go-ahead than ship something unverified that
could destabilize the one feature everything else depends on. Happy to
build it if you want to accept that risk -- just say so.

**5 -- the "Debugging connection was closed" error at the end of a run.**
That error is Browserbase's own embedded page reacting to its CDP
connection tearing down the instant the session is released -- not
something this page's code produces, so suppressing it *inside* the
iframe isn't an option (same cross-origin wall as above). Instead I made
the backend warn the page a moment *before* that happens: 
`tools/deepsearch_tools.py`'s `_run()` used to clear "a browser is live"
state in the same `finally` block that's still holding the Browserbase
session open; now it calls the new `live_activity.set_closing()` in that
`finally` (while the DB still says a browser is live) and only clears
everything *after* `BrowserToolProvider.__aexit__` has actually released
the session. `server.py`'s `/live/<token>/status` route surfaces this as
a new `closing` field, and the page's JS (`showClosing()`) swaps the
iframe for a clean black screen reading "Compiling your results..." the
moment it sees that flag -- before Browserbase's own disconnect banner
would otherwise have a chance to render. The chain-of-thought log carries
over into this screen (a `lastSteps` variable holds the most recent list)
so it doesn't blank out right as the run wraps up. Verified with a small
test server whose `/status` response changes over time (active for 5s,
then `closing` for 5s, then idle) and confirmed via `page.evaluate` that
the log's 3 prior lines were still showing once the page had switched to
the "Compiling your results..." screen.

### A delegation Messa announces but never actually makes

You reported a real conversation where Messa said "Sending back to
deepsearch to update the cart..." and, separately, "Doing both now --
updating the Chipotle cart... and building your Subway order..." -- and
neither one ever actually happened. No new Browserbase session opened, no
follow-up message ever arrived, nothing. The first delegation in that same
conversation (the original Chipotle order) worked completely normally.

This is architectural, not a bug in this project's code, and I confirmed
it directly rather than guessing: `langchain`'s agent loop (which
`create_deep_agent` builds on) ends a turn the instant the model produces
a response with zero `tool_calls` -- that's `factory.py`'s own comment,
"the classic exit condition for an agent loop." There is no next step
where the model gets to "actually" make the call it just described in
text. So when the model writes "sending this to deepsearch..." as pure
acknowledgment text *without* also including the `task` tool call in that
exact same response, the turn is over right there: the text still goes
out over SMS (which is why you saw it), but the delegation it describes
never happens, with nothing in the logs to flag it as a failure -- it
looks exactly like a normal, successful "I'm working on it" message.

I reproduced this exactly with a fake model forced to return
acknowledgment-only text with no tool call, run through the real
`create_deep_agent` stack (not a hand-rolled substitute): confirmed one
model call, zero deepsearch calls, one text message sent -- matching what
you saw down to "the message still arrives, the action just never
happens."

**The fix is a system-prompt change** (`agents/registry.py`), since this
is fundamentally the model's behavior, not something `cli.py`/`run_turn`
can detect or recover from after the fact (a response with no tool call
carries no signal that a delegation was *supposed* to be there -- there's
nothing to catch). The prompt now spells out, explicitly, that the
acknowledgment and the tool call must be the same response, that there is
no later turn to catch up in, and that multiple simultaneous asks (like
your Chipotle-update-plus-Subway-order message) should become multiple
`task` tool calls in one response rather than one now and one "later"
that never comes.

Worth being direct about the limits here: this makes the failure less
likely by making the instruction impossible to misread, but it can't
*guarantee* a model never does this again -- it's steering model behavior,
not fixing a deterministic bug. If you keep seeing it after this change,
the next lever worth pulling is `MESSA_MODEL`/`MESSA_CRITIC_MODEL`
(`config.py`) -- the current default (`deepseek-v4-flash-latest`) is
optimized for speed/cost, and a slower, more instruction-following model
for the orchestrator specifically would likely drop this failure rate
further, at the cost of a slower/pricier reply on every turn. Happy to
help evaluate that tradeoff if it keeps happening -- didn't want to swap
your model out from under you without asking first.

### Messa runs on a stronger model now; every subagent stays on the fast one

Per your call: Messa (the orchestrator) and every subagent used to share
one model instance. Now there are two -- `config.ORCHESTRATOR_MODEL_NAME`
(env: `MESSA_MODEL`, defaults to `~deepseek/deepseek-v4-pro`) for Messa
only, and `config.SUBAGENT_MODEL_NAME` (env: `MESSA_SUBAGENT_MODEL`,
defaults to the same `~deepseek/deepseek-v4-flash-latest` you were already
running) for deepsearch and all four dict-based subagents
(executive_assistant, email_agent, document_agent, routines_agent).

Why split it this way rather than upgrading everything: the "empty
promise" bug above is specifically about Messa's own instruction-following
-- pairing an acknowledgment with its tool call, in one response, no
second chances. That's exactly what a stronger model buys you. The
subagents' job is narrower (execute the one task they were just handed),
so they're less exposed to that specific failure, and putting all 5-6
agents on the pricier model would multiply cost for not much reliability
gain where it matters less. `deepagents`' own `SubAgent` spec supports a
per-subagent `model` override (`registry.py` now sets `"model":
subagent_model` on each of the four dict subagents, and passes
`subagent_model` straight into `build_deepsearch_subagent`) -- this isn't
a workaround, it's the framework's own supported mechanism for exactly
this.

One thing I couldn't verify from here: `deepseek/deepseek-v4-pro` is a
real, current OpenRouter model (confirmed via their docs -- "designed for
advanced reasoning, coding, and long-horizon agent workflows"), but I
can't make a real OpenRouter API call from this sandbox to confirm the
exact string resolves cleanly (no real credentials touch this environment,
by design). If your first real message to Messa comes back with a model
error, the fix is just changing `MESSA_MODEL` -- try
`deepseek/deepseek-v4-pro` without the leading `~`, or the dated snapshot
`deepseek/deepseek-v4-pro-0813` -- no code change needed either way.

### Session continuation now covers "fix what you just did," not just "finish what you didn't"

You asked about this directly: deepsearch's session-resume (`db.get_deepsearch_session`
+ the `_SESSION_REF_RE` "session #<id>" matching in `deepsearch_tools.py`)
was never broken -- it already works for a *completed* session, not just
an unfinished one, because the lookup doesn't filter by status. The gap
was narrower: the system prompt only ever *told* Messa to use it for the
unfinished case ("hit its step limit"). For your Chipotle order, she'd
just finished a *completed* session, and the prompt gave her no nudge to
reference it again when you asked her to swap the drink and add chips --
so any follow-up delegation (had one actually fired) might have started
totally fresh, without deepsearch knowing what cart it was supposed to be
adjusting instead of building from nothing.

The prompt now says explicitly: reference "session #<id>" any time a new
ask continues, corrects, or adds to something deepsearch just did --
finished or not -- and only skip it for a genuinely new, unrelated task.
This is separate from (and doesn't fix) the empty-promise bug above --
this only matters once a delegation actually happens -- but it's a real
gap your question surfaced, so it's fixed alongside it.

### The empty-promise bug is now caught structurally, not just steered away

Even on the stronger orchestrator model, you hit the same failure again:
"Checking both now -- distance from Jax Beach to the Medtronic spot in
Jacksonville, and today's top 3 news. Give me a few." went out, and
nothing ever followed -- same shape as the bug above, just a different
instance of it. That's expected, not a regression: the section above was
explicit that a system-prompt fix "makes the failure less likely... but
it can't *guarantee* a model never does this again -- it's steering model
behavior, not fixing a deterministic bug." This is that prediction coming
true, not the fix failing.

You also asked whether we're bloating Messa with too many tokens, and
whether trimming history or gating it behind a relevance check would
help. I measured it rather than guessing: the fixed system prompt plus
the `task` tool's subagent descriptions plus Messa's own tools come to
roughly 2,000 tokens on every single turn, regardless of history length.
20 short SMS-length history messages added maybe another 600-800 tokens
on top of that -- so a typical turn is somewhere around 3,000-3,500
tokens total. That's nowhere near context-limit territory for the model
in use, and well short of where "lost in the middle" effects typically
start showing up. So: probably not what caused this. I trimmed history
from 20 to 12 messages anyway (`cli.py`'s `run_message`) since it's free
and can only reduce how much an older, unrelated exchange competes for
the model's attention -- but I'd be misleading you if I said it's likely
to fix this specific failure, so don't expect it to. I'd also recommend
against the "relevance check" idea: it means running a whole extra model
call before *every* message just to decide what to include, which adds
real cost and latency to every single turn to solve a token count that
isn't actually large -- worse trade, not better.

What I did add is a deterministic backstop, because relying on the prompt
alone clearly isn't enough by itself. `cli.py`'s `run_turn` now watches
its own turn as it streams: if the *entire* turn makes zero tool calls
and the final text matches the same acknowledgment phrasing the system
prompt tells Messa to use right before a delegation ("checking...",
"give me a few", "one moment", "on it," etc.), that's a strong signal a
call was promised and dropped -- so it replays the same messages plus one
corrective nudge ("your previous reply didn't include the tool call it
described...") and retries exactly once. The original acknowledgment may
already have gone out over SMS before this check runs, which is fine --
it was true when Messa said it, just unfinished; the retry's job is
making sure the actual delegation (and eventually a real answer) follows
it, instead of leaving the request silently dropped like you saw. Bounded
to one retry via an internal `_allow_retry` flag on the recursive call,
so a model that drops the ball twice in a row doesn't loop forever -- it
gives up gracefully and still delivers whatever text the model produced
the second time.

Verified with two tests against the real `create_deep_agent` stack (not a
hand-rolled fake): one where the retry's nudge succeeds (a fake model
that drops the tool call on the first call, then includes it after seeing
the nudge -- confirms the delegation actually fires and a real answer
reaches the user this time) and one where it doesn't (a fake model that
never attaches a tool call, proving the retry gives up after exactly one
attempt rather than hanging). Also re-ran all four of the earlier
live-link/empty-promise tests to confirm this didn't change any of the
still-correct, no-retry-needed behavior they cover.

Same honesty caveat as before, though: the stall-detection regex is a
heuristic matched against the acknowledgment phrasing the prompt asks
for, not a guarantee it catches every possible phrasing a model might
use to describe a dropped delegation. It directly catches the exact
pattern from both real reports so far. If a future instance says
something a genuinely different way and slips past it, that's the next
phrase to add to `_STALL_PATTERN` in `cli.py`.

**Follow-up from testing this live:** you confirmed the retry itself
works, but flagged that the live-view link came through twice in one
exchange -- "first time I was fine but it happened twice and it was a
little annoying." That's a direct consequence of two things being
individually correct but not accounting for each other: `_on_ai_message`
attaches the link to *every* deepsearch delegation's acknowledgment
(deliberately -- see "Phase 3" above, added so a genuinely separate
double-delegation, like the original Chipotle-cart-plus-Subway-order
case, always gets the link on each part), and the retry above can itself
produce a second acknowledgment moments after the first. Neither behavior
was wrong on its own, but together they meant the exact same permanent
link could go out twice within seconds of itself, which reads as spammy
rather than helpful. Fixed with a `live_link_sent` flag scoped to one
`run_message` call (i.e. one incoming text) -- the link is attached to
whichever deepsearch delegation happens first in the turn, and suppressed
on any later one, retry-triggered or a genuinely separate second ask
alike. It's still there the moment a new incoming message starts a fresh
turn. Verified with a test that fires two real, unrelated deepsearch
delegations in one turn (the original Chipotle+Subway shape) and confirms
exactly one link across both acknowledgments, alongside all five previous
tests re-run to confirm nothing else changed.

## Executive assistant rebuild: approval scope, timezone correctness, a small-loop quality check

You asked for an audit of every executive_assistant tool (reminders,
scheduling, tasks, contacts) plus three specific changes: approval required
only for scheduling (tasks and reminders should NOT need approval -- you
corrected this mid-conversation from an earlier draft that also gated
tasks/reminders, so double-check this matches what you meant); heavy
attention to timezone correctness, using the city collected at onboarding
to resolve it, and asking for a zip/city when that's not enough; and a
rebuild of executive_assistant as something suited to personal-assistant
work rather than deepsearch's ~100-step research loop -- "quality and
proper checking within small loops" instead of a large recursion budget.

### The timezone bug, precisely

You reported: "the llm works in UTC and we show results to the user in
whatever timezone they are, without proper setup there will be confusion."
That's actually three separate, compounding bugs, all fixed together in
the new `messa/timeutil.py`:

1. **Write side.** `db._parse_dt` took a naive datetime string from the
   model -- exactly what "3pm tomorrow" looks like once it's in a tool
   call -- and silently assumed it was already UTC. A user in Pacific time
   asking for "3pm" got it stored as 3pm UTC (7-8am Pacific), with nothing
   anywhere flagging the mismatch. Fixed by `timeutil.to_local_aware`,
   which every date/time-carrying tool in `executive_tools.py` now runs
   input through before it reaches the database: a value with no explicit
   UTC offset is interpreted as the *user's own* local time.
2. **Read side.** `list_tasks`/`list_reminders`/`list_calendar_events` just
   stringified whatever asyncpg returned for a `timestamptz` column (which
   asyncpg normalizes to UTC) with no conversion and no zone label at all
   -- so even a *correctly stored* time looked wrong once displayed. Fixed
   by `timeutil.format_local`, used everywhere a stored date/time is shown
   back to the user (e.g. "Aug 28, 2026 01:00 PM PDT" instead of a bare
   UTC timestamp).
3. **The timezone itself was never real.** `users.timezone` was written
   once, at account creation, to a hardcoded default
   (`MESSA_DEFAULT_TIMEZONE`) and never derived from the city onboarding
   actually collects -- so even with (1) and (2) fixed, the timezone being
   used could still just be wrong for that specific person. Fixed by
   wiring real resolution into the onboarding flow: `save_profile_field`
   ('city') now calls `timeutil.resolve_timezone(city)` and stores the
   result, and a new `users.timezone_confirmed` column
   (`migrations/007_timezone_confirmed.sql`) tracks whether that ever
   actually succeeded, as opposed to still being the placeholder.

No system prompt anywhere (Messa's or any subagent's) used to state the
actual current date/time or the user's timezone at all -- the model had no
deterministic anchor for "what time is it right now." `_build_system_prompt`
in both `agents/registry.py` and `tools/executive_tools.py` now opens with
`timeutil.current_context_str`, an explicit line like "Current date/time:
Wednesday, August 27, 2026 02:15 PM EDT (timezone: America/New_York)" --
and, when `timezone_confirmed` is false, an explicit instruction to ask
for a city/zip before treating anything time-sensitive as settled.

### Resolving a city (or zip) to a real timezone

`timeutil.resolve_timezone` uses Open-Meteo's free, keyless geocoding API
(`https://geocoding-api.open-meteo.com/v1/search`), which returns an IANA
timezone directly for both city names and US zip codes -- no separate
lat/lon lookup needed. Verified live by hand during development (three
manual fetches, since this project's own dev sandbox restricts outbound
network calls to a small package-registry allowlist and couldn't run the
real HTTP call itself):

- A bare `"Jacksonville"` (no state) returns results in **three different
  US timezones** (FL/NC/IL) -- a real ambiguity hazard for a bare city
  name.
- A 5-digit US zip (`"32218"`) resolves to exactly one unambiguous result.
- Even `"Jacksonville, FL"` included one low-population outlier (a police
  heliport) tagged with a *different, wrong* timezone for that county.

`resolve_timezone` handles this by preferring populated results and
checking whether the highest-population candidates actually agree on
timezone before calling the result "confident." When they don't agree (or
nothing resolves at all), the caller is told so explicitly -- both
`save_profile_info`'s tool result (which tells Messa to ask for a zip code
or "city, state") and the onboarding prompt itself
(`_ONBOARDING_PROMPTS["awaiting_location"]`, updated to ask for a state/zip
up front rather than a bare city name) reflect this. An existing user
created before this feature existed (or whose city never resolved) isn't
stuck: `db.ensure_timezone_resolved`, called on every `load_user_context`,
retries resolution against the city already on file until it succeeds,
with no user action needed.

**Honesty caveat:** the live Open-Meteo call itself could not be
exercised end-to-end from this sandbox (see above) -- only the decision
logic (population weighting, agreement checking) is covered by an
automated test, against the exact response shapes recorded from the three
manual fetches. Any normal deployment target (HuggingFace Spaces included)
has unrestricted outbound HTTPS, so the real call is expected to work as
designed; this is the one thing I genuinely could not verify myself before
shipping it.

### Approval scope, corrected: only scheduling

Your first draft said approval should be required for "scheduling,
reminders, tasks," with "others" (notes, contacts) going through
unapproved. Partway through, you corrected that to: approval only for
scheduling -- reminders and tasks should NOT need approval either. The
final, shipped scope is the corrected one:

| Action | Approval? |
|---|---|
| Calendar events (create/update/delete) -- "scheduling" | **Yes** -- `propose_*` -> `pending_actions` -> Messa confirms |
| Recurring automations (routines_agent's cron jobs) | Yes -- same reasoning, a standing schedule is the same category of risk |
| Tasks (create/update/delete) | No -- direct write |
| Reminders (create/cancel) | No -- direct write |
| Notes (create/delete) | No -- direct write (delete used to be gated; that's removed) |
| Contacts (create/update/delete) | No -- direct write (**delete didn't exist at all before** -- see below) |

The reasoning: tasks/reminders/notes/contacts are low-stakes and trivially
reversible with one follow-up message ("cancel that reminder"); a calendar
event is the one category where a mistaken write is more likely to
visibly collide with a real commitment someone else can also see (a
meeting, an appointment), so that's the one still kept behind an explicit
human yes. `db.py`'s `_APPLIERS`/`GATED_ACTION_TYPES` now contains exactly
`create_calendar_event`, `update_calendar_event`, `delete_calendar_event`,
`create_recurring_cron` -- everything else that used to route through
`pending_actions` (`create_task`, `update_task`, `delete_task`,
`create_reminder`, `cancel_reminder`, `delete_note`) is now a plain direct
function, and the old conn-taking `_insert_task`/`_update_task`/etc.
appliers are gone rather than left dead in the file.

While auditing "if I missed any, you still take care of it," I found
**contacts had no delete at all** -- `upsert_contact` could create/update a
person, but nothing could remove one. Added `db.delete_person` /
`tools/executive_tools.py`'s `delete_contact` (matches by name, direct, no
confirmation, consistent with the rest of the contacts surface).

### executive_assistant rebuilt as a small-loop CompiledSubAgent, not a deepsearch clone

Previously executive_assistant was one of the four plain declarative
`SubAgent` dicts (name + system_prompt + tools), sharing deepagents'
generic subagent path with no custom recursion limit -- meaning a confused
run could, in principle, wander for as many steps as the orchestrator's
own 100-step budget allows. That's the wrong shape for "create a task" or
"set a reminder," which is a handful of direct DB calls, not an
open-ended research loop.

`tools/executive_tools.py`'s `build_executive_subagent` now mirrors
deepsearch's `CompiledSubAgent` pattern (a hand-written `_run()` wrapping
its own `create_agent(...)` call with an explicit `run_config`) instead,
with two differences that are the actual point:

1. **A small, dedicated recursion budget** -- `config.EXECUTIVE_RECURSION_LIMIT`
   (default 14, overridable via `MESSA_EXECUTIVE_RECURSION_LIMIT`), not
   `DEEPSEARCH_MAX_STEPS` (100). Enough for several tool calls plus a
   summary; not enough for a runaway loop.
2. **A deterministic, non-LLM quality check**, not a second unconditional
   LLM "critic" call. `build_executive_tools` optionally threads a
   `written` list through every date/time-writing tool; after the inner
   agent finishes, `_past_due_issue` checks whether anything just written
   landed more than 5 minutes in the *past* relative to the user's real
   local now -- almost always a parsing/timezone mistake, not an
   intentional backdated entry. If so, exactly one corrective nudge is
   sent back into the *same* LangGraph thread (via the checkpointer, so
   the model still has full context of what it just did) before the
   delegation's reply goes back to Messa -- the same bounded
   retry-once shape already proven for the orchestrator's own
   dropped-delegation bug (see "The empty-promise bug" above), just
   applied to a different failure mode.

Verified against the real `langchain.agents.create_agent` + LangGraph
stack (only the chat model and `db.py`'s reminder functions are faked): a
fake model creates a reminder for a date years in the past, wrongly
believes it's done, gets the auto-check nudge, and correctly cancels the
stale reminder on the retry -- exactly 2 rounds (4 model calls total), not
more, proving the retry is bounded to once. A separate test exercises
`build_executive_tools` directly (no LLM) to confirm the read-side
timezone display fix, the write-side interpretation fix, and that only
`propose_create_calendar_event` (and its update/delete siblings) ever call
`db.propose_action` while everything else calls a direct function.

### Reminders and recurring jobs actually reach a real phone now

While auditing "make sure reminders... are functioning as they should," I
found a gap that wasn't explicitly asked about but falls squarely under
that instruction: `messa/background.py`'s pollers (which check for due
reminders/cron jobs) were only ever started from `cli.main_async` --
**never from `messa/server.py`**, the actual production webhook app. In
production, a due reminder was being correctly identified in the database
and then... nothing happened with it. The CLI's poller only prints to a
local terminal (`console.proactive`) and its cron loop only logged "would
run now" without invoking Messa at all -- fine as a Phase 1 preview of the
scheduling math, but not something that was ever going to deliver an SMS.

`server.py` now starts two real pollers on FastAPI startup:
`_production_reminder_loop` sends a due reminder via
`sendblue.send_message` and only marks it sent once delivery actually
succeeds (a Sendblue failure leaves it pending so the next poll retries
rather than silently losing it); `_production_cron_loop` re-invokes Messa
for real via the same `load_user_context`/`build_orchestrator`/
`run_message` path a live inbound text uses, with the job's saved
`prompt_or_task` as the message, and delivers whatever Messa produces back
over Sendblue. `db.get_due_reminders_for_delivery` /
`get_due_cron_jobs_for_delivery` are new query functions that join
`users` for the `phone_number` a real send needs (the existing
`get_due_reminders`/`get_due_cron_jobs` stay as-is for the CLI's
console-only preview). `messa/background.py`'s docstring is updated to
point at this as the real counterpart, and `routines_agent`'s system
prompt/docstring no longer says a confirmed job "isn't actually executed
yet" -- it is, now, in production.

## Deepsearch speed: evaluating your researched tools

You and other users reported deepsearch feeling slow, and separately asked
about a visible cursor on the live view. You'd researched seven specific
tools/techniques and asked me to evaluate and implement them. Here's what
I found, verified where I could, and did -- three implemented now (no new
architecture, no new risk), one implemented in a deliberately narrower
form than proposed, one I'd recommend against, and one still open (the
cursor) because it's got a real trade-off against the speed goal you also
asked for -- see the question at the end of this section.

### What I verified, and how

Before touching anything, I checked what our actual dependency already
supports, rather than assuming from documentation. `@playwright/mcp` (the
exact package deepsearch already runs, `--help` output captured against
the pinned `@latest` version) turns out to already implement most of what
you researched -- it just wasn't configured or prompted to use it. I also
spun up a real headless instance of it in this sandbox (using the
pre-installed Chromium) and called `browser_snapshot`/`browser_find`
directly to see actual tool schemas and the actual `ref` format (`e6`,
`e7`, ...) rather than guessing -- a live public site couldn't be reached
from here (same sandbox network restriction as everywhere else in this
project), but the tool schemas and ref format came back real and exact.

### 1. Accessibility Tree (AXTree) pruning -- already how we work, tuned further

Deepsearch was never vision-based -- `browser_snapshot` already returns an
accessibility tree, not a screenshot, which is the whole idea behind this
suggestion. What WAS missing: `@playwright/mcp` ships a purpose-built
`browser_find(text=... / regex=...)` tool -- "cheaper than capturing the
whole snapshot when you only need to locate an element" -- and
`browser_snapshot` itself takes a `depth` argument for a shallower tree,
but `DEEPSEARCH_SYSTEM_PROMPT` never told the model either existed, so it
always reached for a full snapshot. Both are now called out explicitly in
the prompt. Also added two CLI flags on the `@playwright/mcp` subprocess
launch (`tools/deepsearch_tools.py`'s `BrowserToolProvider.__aenter__`):
`--image-responses omit` (never embeds a base64 screenshot in a tool
result -- our subagent model isn't confirmed to accept image input at
all, so that would be pure wasted tokens today) and a tuned-down
`--timeout-settle` (`config.DEEPSEARCH_TIMEOUT_SETTLE_MS`, default 200ms
vs the package's own 500ms default -- an unconditional per-action wait,
so this is a real, if modest, latency cut on every single click/type/
navigate). **Caveat:** I couldn't verify the settle-time change against a
live Browserbase session (no live account here) -- if you see stale-
snapshot-looking flakiness after this on slower sites, raise it back
toward 500 via `MESSA_DEEPSEARCH_TIMEOUT_SETTLE_MS`.

### 2. Vision + Set-of-Marks navigation -- available, NOT enabled by default

`@playwright/mcp` also already has this built in (`--caps=vision`, adding
coordinate-click tools like `browser_mouse_click_xy`) -- Playwright's own
docs are explicit that it "trades efficiency for coverage": more tokens
(screenshots) and slower, used only for elements a page's accessibility
tree doesn't expose at all (canvas UIs, some custom widgets). I did NOT
enable it: your subagent model (`config.SUBAGENT_MODEL_NAME`) isn't
confirmed to accept image inputs on OpenRouter, and coordinate-click tools
are useless without a model that can actually look at the screenshot
first -- turning this on today would just add tool-schema token overhead
for a capability the model can't use, the opposite of the goal. If you
confirm/switch to a vision-capable subagent model later, this is a
one-flag addition (`--caps vision`) plus a prompt note to use it only as
a last resort when accessibility-tree navigation genuinely fails.

### 3. Stagehand by Browserbase -- recommend against, for now

Verified: Stagehand does have first-class Python, TypeScript, and Go SDKs
with matching `act`/`extract`/`observe`/`agent` primitives, and does
offer server-side caching of successful actions (its real speed/cost
lever -- skipping the LLM call on a repeat action against a page it's
seen before). But three things weigh against adopting it right now: (a)
`stagehand-py` on PyPI is explicitly self-described as "an early release"
seeking feedback -- meaningfully less mature than the TypeScript SDK; (b)
it requires `BROWSERBASE_PROJECT_ID` in addition to the API key we
already use, a new required config value; (c) most importantly, adopting
it means replacing deepsearch's tool layer entirely with Stagehand's own
primitives -- which directly reverses the explicit call you made when we
moved deepsearch onto Browserbase (see "Browserbase" above): "This
deliberately does *not* use Browserbase's own Stagehand/agent product...
Keeping your own fine-tuned agent driving the browser: yes, fully
preserved." I didn't want to quietly walk that back. The caching idea
itself is worth revisiting later if repeat-task volume grows (the same
grocery-cart-style request happening often for the same user/site is
exactly Stagehand's best case) -- but as a deliberate future option, not
something I built into this pass.

### 4. Direct Playwright Role/Text selectors -- already supported, guard relaxed to use it

This was the single biggest concrete win, and it needed zero new
dependencies: `browser_click`/`browser_type`/`browser_hover`/
`browser_select_option`/`browser_drag` already accept a `target` that's
EITHER an opaque snapshot ref ("e12") OR, per the tool's own schema,
"a unique element selector" (`role=button[name="Submit"]`, `text=Submit`,
a CSS selector) -- resolved fresh by Playwright on every call. Our own
guard layer (`SNAPSHOT_DEPENDENT_TOOLS` in `deepsearch_tools.py`) was
needlessly requiring a fresh `browser_snapshot` before EVERY one of these
calls, even when the model already had a perfectly stable selector and
didn't need a ref at all -- forcing an unnecessary full-snapshot round
trip before every single interaction. `_requires_fresh_snapshot` now only
enforces that requirement when the target actually looks like an opaque
ref (verified live: always exactly `e<number>`, via `_SNAPSHOT_REF_RE`) --
a selector-style target skips the check entirely, since Playwright
resolves it fresh regardless of what changed on the page. The system
prompt now tells the model to reuse a stable selector directly for a
repeat interaction instead of re-snapshotting. Covered by a unit test
against the exact ref/selector shapes involved.

### 5. Hybrid non-browser lookup tool -- implemented, given to Messa directly

This is the other big win, and probably the most impactful for typical
usage: a lot of what gets delegated to deepsearch is a plain lookup ("what's
the score", "who is X", "what does this article say") that doesn't need a
browser at all -- and deepsearch pays a real, fixed cost before it does
anything useful (opening a Browserbase session, launching `@playwright/mcp`
over CDP, a full ReAct loop with the whole Playwright tool schema loaded).
New `messa/tools/web_search_tools.py` gives Messa two direct tools of her
own -- `web_search` (DuckDuckGo, via the actively-maintained `ddgs`
package, no API key) and `fetch_page_text` (plain HTTP GET + HTML-to-text,
via `httpx`+`beautifulsoup4`) -- and her system prompt now tells her to
use these herself for a plain factual lookup, reserving deepsearch for
anything that actually requires clicking, forms, logins, or JS-rendered
content a plain fetch can't see. Deliberately placed on MESSA's own
toolset, not nested inside deepsearch -- the whole point is deciding
"does this need a browser" *before* one ever opens; putting it inside
deepsearch would mean the Browserbase session is already open by the time
that decision gets made. Both tools fail closed with a message that
explicitly points back to deepsearch as the fallback, rather than
silently giving up, when the plain-HTTP approach can't handle a page.
**Caveat**, same shape as this project's earlier geocoding-API work: this
sandbox's own outbound network is restricted to a package-registry
allowlist, so the live DuckDuckGo call itself can't be exercised
end-to-end from here (confirmed live: it genuinely 403s through this
sandbox's proxy) -- verified instead via unit tests against the pure
formatting/extraction logic and the tools' own failure-handling path with
DDGS/httpx faked. Any normal deployment target has unrestricted outbound
HTTPS.

## Cursor visualization: the open question

The other user ask -- a visible cursor moving on the live view -- has a
real tension with the speed work above that I didn't want to resolve on
your behalf without asking. What I verified:

- **ghost-cursor** is real and well-maintained, but it's built around
  direct access to a Puppeteer/Playwright `Page` object (it computes a
  bezier-curve path and dispatches a sequence of real `mouse.move` events
  along it) -- something that only exists inside the Node process actually
  driving the browser. We don't hold that object in our Python code at
  all; we only talk to `@playwright/mcp` over the MCP protocol as a
  black-box tool server. Using ghost-cursor for real would mean either
  forking `@playwright/mcp` to inject it (ongoing maintenance burden
  against a fast-moving upstream project) or writing and maintaining our
  own much smaller custom browser driver in place of it -- a real rewrite,
  not a config change.
- **"Playwright-cursor"** as a distinct, separate package could not be
  found under that name in an npm/GitHub search -- what exists are
  community Playwright *ports* of ghost-cursor itself (`ghost-cursor-
  playwright`, `playwright-ghost-cursor`, a couple of similarly-named
  forks with modest adoption), not a separate, established tool. I'd
  treat "ghost-cursor" and "Playwright-cursor" as the same idea rather
  than two independent options to evaluate.
- Either way, a visible, human-paced cursor is drawn deliberately slower
  than an instant click *by design* (that's what makes it look human/
  visible at all) -- directly in tension with "the agent is too slow,"
  and it can be tuned down, but not to zero without the animation
  becoming imperceptible again.
- There's a cheaper alternative within our CURRENT architecture, no
  rewrite required: `@playwright/mcp` has a documented `--init-script
  <path>` flag (confirmed in the real `--help` output) that runs a JS file
  in every page before its own scripts do. That script could draw a small
  floating dot and move it to a target element's bounding box just before
  each click, entirely inside the page -- no raw CDP mouse events, no
  forked driver. It's real work (a new init script, and a way for our
  guard code to trigger the move -- most likely via `browser_evaluate`,
  which exists today but is currently gated behind approval since it's
  normally model-invoked; using it internally for this would need to
  bypass that gate for this one, non-model-chosen purpose) and it still
  costs a bit of extra time per click, just less than full ghost-cursor-
  style human-motion simulation.

I asked, and you chose the cheap DOM-overlay approach, with an SVG cursor
arrow rather than a plain dot. Implemented:

- **`messa/assets/cursor_overlay.js`**: injected into every page via
  `@playwright/mcp`'s real, confirmed `--init-script` flag (verified in
  the actual `--help` output, not assumed). Defines
  `window.__messaCursor.moveTo(x, y)`, drawing a small blue SVG arrow
  (fixed-position, `pointer-events: none` so it never interferes with the
  page, always on top) that CSS-transitions to a point over ~220ms and
  resolves a Promise once the transition finishes.
- **`BrowserToolProvider._move_cursor_to`** (in `deepsearch_tools.py`):
  called from the guard for `browser_click`/`browser_type`/`browser_hover`/
  `browser_select_option` (`CURSOR_ANIMATED_TOOLS`) right before the real
  action runs, with the SAME `element`/`target` the real action is about
  to use -- so the arrow genuinely goes where the click will land, not
  somewhere approximate. It does this via a raw, internal call to the
  underlying `browser_evaluate` MCP tool -- deliberately bypassing
  `_guard`'s own approval gate entirely (that gate exists for the MODEL
  choosing to run arbitrary JS; this is our own cosmetic, non-model-
  initiated side effect and must never prompt for or be blocked by human
  approval). Any failure here (element not found, page not settled yet) is
  swallowed and logged, never allowed to block or fail the real action --
  this is decoration, not a dependency the automation relies on.
- **`config.DEEPSEARCH_CURSOR_OVERLAY`** (env: `MESSA_DEEPSEARCH_CURSOR_OVERLAY`,
  default `true`): an explicit off-switch, since this is a real, if small,
  added cost per click (one extra internal `browser_evaluate` round-trip
  plus the ~220-260ms transition) -- turning it off skips both the
  `--init-script` flag and every `_move_cursor_to` call entirely, with the
  automation itself completely unaffected either way.

**What this deliberately does NOT do**, consistent with the "cheap" framing
you chose over the full rewrite: no bezier-curve path or multi-step human-
motion simulation (ghost-cursor's actual technique) -- just one clean move
per interaction. `browser_drag` isn't included in `CURSOR_ANIMATED_TOOLS`
(a drag has two endpoints, not one, and animating both cheaply needs a bit
more thought) -- worth revisiting if drag-based flows come up.

**Caveat, same shape as everywhere else in this section:** I could not
verify this against a live Browserbase live-view session from this
sandbox (no live account here) -- I confirmed the injected script's JS is
syntactically valid (`node --check`) and that `_move_cursor_to` calls
through correctly with fakes standing in for the real MCP tool, but
whether Browserbase's live-view screencast visibly renders this overlay
exactly as expected (z-index conflicts with a specific site's own fixed-
position elements, an unusually slow page where the 260ms fallback fires
before the real transition, etc.) can only really be confirmed by watching
a real session. If the arrow doesn't show up or looks off on a specific
site, that's the first thing to check.

**Update -- your customized cursor, kept as-is:** You edited
`cursor_overlay.js` yourself after this landed (bigger 56x56 green arrow
with a glow, an idle "breathing" drift between five resting spots, a
click-press pulse, and a glide back to the next resting spot after every
action) and sent it back over. That customization is now the canonical
version of the file -- I built the reading animation below *on top of* it
rather than replacing it with anything of my own; nothing you changed was
touched. The one thing worth knowing if you tune it further: the reading
animation (next section) calls the exact same `window.__messaCursor.moveTo`
your idle-breathing/click-pulse logic is built around, so any further
changes to `moveTo`'s own behavior automatically apply to both features --
there's no second, separate copy of that logic to keep in sync.

## Reading animation: making "the agent is thinking" visible

The other half of the same request: deepsearch calling `browser_snapshot`
and then just sitting on a static page for several seconds of LLM
think-time before its next action reads as frozen, not working. You asked
for human-like "scroll a little, pause, then scroll more" movement during
that gap, flagged the real design problem yourself (humans center whatever
they're looking at, and a scripted scroll doesn't know what that is), and
asked for any other effects that would make the live view feel like the
agent is actually browsing.

**The centering problem, and why it turned out to be the easy part:** the
trick isn't a scrolling *technique* -- it's picking the right scroll
*target*. A human's gaze-driven scrolling ends up centering content because
they're scrolling to keep looking at something specific, not scrolling by
some fixed number of pixels. So the script picks real content elements off
the page in document order (headings, paragraphs, list items, images,
table cells -- skipping anything too small/hidden or bunched right on top
of the last pick) and scroll-centers each one in turn, using the same math
as `element.scrollIntoView({block: "center"})`. "Centered" falls out
automatically once the target is a real piece of content instead of an
arbitrary offset.

**"Scroll a little, pause, then scroll more,"** taken literally at two
levels:
- Between one piece of content and the next, the scroll itself is split
  into two smaller hops with a short pause between them (a glance, then a
  settle) rather than one smooth glide or an instant jump.
- Between pieces of content, there's a longer "reading" pause, scaled
  (loosely, and clamped) to how much text is in the thing just centered --
  a short list item gets a shorter pause than a long paragraph.

**Tying it to the cursor, for the "unforgettable" ask:** each stop nudges
your existing `__messaCursor.moveTo` toward the content just centered,
which -- because you already built click-pulse and idle-breathing into
`moveTo` -- means the arrow visibly settles near the text and drifts a
little on its own, exactly like someone reading with a mouse in hand
resting near what they're looking at. This reuses your logic rather than
adding a second, competing cursor behavior.

Implementation:

- **`messa/assets/cursor_overlay.js`** -- appended (not replacing your
  cursor code above it) with a second, self-contained
  `window.__messaReader = { start(maxTargets), stop() }`. `start()` picks
  up to `maxTargets` content elements and works through them with the
  hop/pause/read pattern above; calling `start()` again while one is
  already running is a safe no-op (`"already-running"`) rather than
  stacking two animations. `stop()` just sets a flag the loop checks
  between hops -- it doesn't try to interrupt an in-flight scroll
  instantly, which is what keeps it cheap to call.
- **`BrowserToolProvider._start_reading_animation`** (in
  `deepsearch_tools.py`): fired the instant a `browser_snapshot` call
  succeeds, as a plain `asyncio.create_task(...)` that is **never
  awaited** -- it wraps one long-lived internal `browser_evaluate` call
  running `__messaReader.start(6)`, entirely in parallel with the agent
  loop moving on to decide its next step. This is the whole point: the
  animation fills think-time that was already going to happen, rather
  than adding any of its own.
- **`BrowserToolProvider._stop_reading_animation`**: called at the very
  top of `guarded()`, for every tool call, not just cursor-animated ones --
  the model choosing to do *anything* next is the signal that "reading" is
  over. Fires one quick internal `browser_evaluate` running
  `__messaReader.stop()` and then forgets about the original background
  task (it's left to wind down and resolve on its own time, typically
  within one more hop -- waiting for it here would reintroduce exactly the
  latency this feature isn't supposed to add). A no-op with zero extra MCP
  calls when nothing is currently running, so a normal tool call pays no
  cost for a feature that never fired that turn. `__aexit__` cancels
  anything still outstanding as a last resort if the whole browser session
  tears down first.
- **`config.DEEPSEARCH_READING_ANIMATION`** (env:
  `MESSA_DEEPSEARCH_READING_ANIMATION`, default `true`): shares the same
  `--init-script` injection as the cursor overlay (one asset file, one
  flag -- the flag is only *omitted* if both features are off) but is
  independently toggleable from `DEEPSEARCH_CURSOR_OVERLAY`.

**The concurrency question this design actually depends on, verified
empirically rather than assumed:** a long-running background
`browser_evaluate` call and a short, separately-dispatched one sent while
the first is still in flight -- does `@playwright/mcp` pipeline them over
its stdio JSON-RPC transport, or queue the second behind the first? If it
queued them, firing the animation as a background task wouldn't actually
keep the real action responsive; the real click's own `browser_evaluate`-
backed cursor move (or the stop signal itself) could sit stuck behind the
animation's multi-second call regardless of what our Python code does. I
tested this directly against a real local headless Chromium (no network
needed, consistent with this sandbox's restricted egress): a 3-second
in-page `browser_evaluate` call and a near-instant one fired 300ms later
both resolve independently -- the fast one completes in well under a
second, not after the slow one. Confirmed again with the actual reading
animation itself: polling `scrollY` every 500ms while `__messaReader.start()`
ran in the background stayed fast throughout, and a `stop()` call issued
mid-animation returned in ~0.5s rather than waiting for the running
animation to finish. Both scripts are included if you want to re-run them
against a real Browserbase CDP endpoint later:
`/tmp/test_mcp_concurrency.py` and `/tmp/test_reading_animation_live.py`
(the latter also exercises the actual scroll-and-stop mechanics end to end
against synthetic long-form content, not just the concurrency question).

**What this deliberately does NOT do:** it isn't a model of real reading
speed (the pacing is a clamped, roughly-proportional visualization, tuned
for watchability on a live view, not accuracy) and it doesn't try to
reproduce natural eye movement (saccades, re-reading, skipping around) --
just a small, distinct number of stops in document order. It also doesn't
run at all while a real action is pending or in progress -- it's strictly
a "the agent looks busy while it's actually just thinking" effect, never
layered on top of a genuine click/type/navigate.

**Caveat, same shape as the cursor section above:** everything here was
verified against a real local headless Chromium in this sandbox (the
mechanics: injection, scrolling, centering math, the stop signal, the
concurrency behavior it depends on), but not against an actual Browserbase
CDP-remoted session or a real, visually complex website -- a very tall
single-page app with lazy-loaded content, or a page that itself listens for
`scroll` events and reacts to them, could behave differently than the
synthetic test page here. If the animation looks off on a specific site,
that's the first place to look.

## Real human-cursor, multi-site delegation, and human-in-the-loop pause

Your three-part ask, in the order I shipped it (each stage independently
testable, later ones building on the HTTP-transport refactor from stage 2):
(1) a reliable, cost-bounded pause when deepsearch hits a login wall or
CAPTCHA, instead of getting stuck or silently giving up; (2) splitting a
multi-site task across a few concurrent sub-agents instead of visiting sites
one at a time; (3) swapping the DOM-drawn cursor arrow above for **real**
`human-cursor` mouse movement. All three land in
`messa/tools/deepsearch_tools.py`, because (2) and (3) both needed the same
underlying change first -- more than one browser tab in a single deepsearch
run -- so they share one plumbing layer.

Before committing to a design I prototyped the riskiest pieces directly in
this sandbox (scripts kept under `/tmp/` for reference/re-running):
`@playwright/mcp` run in HTTP mode really does give each separate client
connection its own isolated tab; `langchain.agents.create_agent` (what this
file already uses) really does run multiple tool calls from one AI turn
concurrently, so a model calling a delegation tool several times in one turn
gets real parallelism for free; and the real `human-cursor` npm package
really can drive genuine mouse events from a *second*, independent CDP
connection into a browser some other process (here, `@playwright/mcp`) is
already driving -- confirmed via a page-side event counter, not just "no
error was thrown." Full details and caveats for each stage below.

### 1. Human-in-the-loop pause: reliable, notifies you, and bounded on cost

New tool, `request_human_help(reason)`, on every tab's toolset (top-level
and every delegated sub-worker). `DEEPSEARCH_SYSTEM_PROMPT` tells the model
to call it when it recognizes a login wall, CAPTCHA, or 2FA/one-time-code
prompt -- and there's a deterministic backstop too: `guarded()` already
tracks consecutive tool failures, and after two in a row on a page whose
last snapshot text matches an obvious login/verification keyword, the error
message itself nudges the model toward calling the tool instead of retrying
forever (same "one corrective nudge into the same graph thread" shape
already used for `executive_assistant`'s quality check -- not a new
mechanism).

You specifically asked for this to be a **reliable flow, not the browser
timer controlling everything** -- so the actual mechanics are deterministic
and DB-backed, not left to one in-flight LLM call to get right:

1. `request_human_help` writes a durable row to a new table
   (`migrations/008_deepsearch_human_help.sql` --
   `deepsearch_human_help_requests`) and sets a "waiting for you" flag the
   live-view page can show.
2. A **separate background loop** in `server.py`,
   `_production_deepsearch_pause_loop()` -- same `asyncio.sleep`-polled
   shape as the existing reminder/cron loops, started and cancelled
   alongside them -- notices the new row within about 5 seconds and sends
   **one** fixed-template Sendblue text ("I need your help finishing up --
   \<reason\>. Jump into the live view: \<link\>"), then marks it notified
   so it never double-sends. Deliberately a fixed template, not a
   re-invoked Messa LLM call: reliability was the explicit ask, and
   re-invoking an LLM mid-flight is exactly the kind of extra failure
   surface this flow shouldn't depend on (see the empty-promise
   delegation-dropping bug from an earlier round of this project).
3. **The actual wait/extend/give-up loop runs inside `request_human_help`
   itself**, since only it has direct access to that tab's own MCP
   connection: polls every few seconds for up to
   `DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS` (60s, your number) with no
   activity at all before giving up. Real activity (the page's URL changing,
   or text appearing/changing in an input field -- see
   `_page_fingerprint`'s comment for why that's encoded as a single packed
   number, not a URL string) extends the wait in
   `DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS` (20s) increments, capped
   regardless by `DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS` (300s = 5
   minutes, your number) -- that hard cap, not the "looks active" heuristic,
   is what actually bounds the cost. A URL change is treated as resolved
   (logins/verifications almost always redirect on success): the tool
   returns a fresh snapshot and hands control back to the model. A timeout
   returns a `BLOCKED:` string telling the model to move on to other
   independent work if there is any, or wrap up and report honestly that
   this part needs you.
4. **Belt-and-suspenders session cap**, independent of all of the above:
   the whole inner agent run is wrapped in
   `asyncio.wait_for(..., timeout=config.DEEPSEARCH_MAX_SESSION_SECONDS)`
   (1200s = 20 minutes) in `build_deepsearch_subagent`'s `_run`, so even a
   bug in the pause logic above can't hold a Browserbase session open
   indefinitely. This is the actual answer to "we don't want a rogue
   browser session eating up our finances" -- enforced one level above the
   feature that's supposed to prevent it, not only inside that feature.

Verified with fakes for the MCP calls and the DB (`/tmp/test_human_help.py`
-- the give-up-with-no-activity, extend-then-resolve-on-URL-change, and
hard-cap-enforced-even-with-continuous-activity paths, plus the
deterministic backstop nudge) and against a real local headless Chromium
(`/tmp/test_fingerprint_live.py`, including the exact mixed-quotes edge
case that broke an earlier, string-based version of the page fingerprint --
see that method's own docstring) and a real `server.py` poll loop
(`/tmp/test_pause_loop.py`).

### 2. Multi-site delegation: up to `DEEPSEARCH_MAX_SUBAGENTS` tabs at once

New tool, `delegate_website_task(url, instructions)`, on the top-level
deepsearch toolset only (a sub-worker cannot itself delegate --
`BrowserToolProvider._owns_server` enforces that directly, not just by
leaving the tool off a sub-worker's list, so there's no path to recursive
delegation even by accident). Call it multiple times in the *same* turn for
genuinely independent multi-site work (e.g. comparing a price across three
sites) and, per the concurrency behavior confirmed above, they actually run
in parallel -- `DEEPSEARCH_SYSTEM_PROMPT` tells the model this explicitly,
and also tells it NOT to use this for a single site or for steps that
depend on another delegation's result.

Mechanically: the top-level `BrowserToolProvider` no longer runs
`@playwright/mcp` over stdio. It spawns it as a local HTTP server instead
(`--port 0 --shared-browser-context --isolated`, plus the existing
`--cdp-endpoint <Browserbase connectUrl>`) and connects to it as client #1.
`delegate_website_task` opens a **new**, independent HTTP client connection
to that *same already-running* server -- no new `@playwright/mcp` process,
no new Browserbase session -- which lands it on its own isolated tab, runs
a small `create_agent` loop scoped to exactly that one site (a trimmed
system prompt, bounded by `DEEPSEARCH_SUBAGENT_MAX_STEPS`, smaller than the
top-level's own step budget), and returns its summary. Every call is
bounded by an `asyncio.Semaphore(config.DEEPSEARCH_MAX_SUBAGENTS)` shared
for the whole run, so calls beyond the cap simply wait for a free slot
rather than erroring -- no reliance on the model counting concurrency
itself.

**On your question of "5 sub-agents at once" -- I set `DEEPSEARCH_MAX_SUBAGENTS
= 3` for now**, matching what you actually picked when asked directly:
start lower, raise later. Each concurrent tab is another live browser
connection (and, with real human-cursor below, another Node-side cursor
instance), so the cost of the cap being wrong is a reliability-and-cost
question, not just a performance knob -- easy to raise by changing one
constant (or the `MESSA_DEEPSEARCH_MAX_SUBAGENTS` env var) once this has
run for real.

**A real bug this surfaced, worth knowing about:** the first version of
this used `--shared-browser-context` alone (matching the throwaway POC that
validated the idea, `/tmp/test_mcp_multitab.py`) and it was NOT enough once
a real `--cdp-endpoint` was involved -- two concurrent connections raced
onto the very same page (one connection's navigation was literally
"interrupted by another navigation" from the other). The POC didn't catch
this because it let `@playwright/mcp` launch its own local browser rather
than connecting to an externally-owned one, which is exactly how a real
Browserbase session works. Adding `--isolated` fixed it -- confirmed with a
dedicated flag-comparison probe before landing on the fix, then re-verified
against the real shipped code path. This is the sort of thing I'd still
want watched on a first real run against an actual Browserbase account
(the flag combination is untested there specifically, only against a real
local CDP endpoint standing in for one), but the mechanism itself
(`connectOverCDP` to an arbitrary CDP endpoint) is identical either way.

`messa/live_activity.py` gained a `tabs` dict (per sub-worker: description,
steps, its own waiting-for-human flag) alongside the existing flat fields,
which keep meaning exactly what they meant before -- the top-level tab's
own status. This is deliberately NOT one shared flag: several sub-workers
hitting `request_human_help` at the same time must never race to clobber
each other's "waiting for you" reason. **Heads-up I can't fix from here:** I
only have the backend `/live/<token>/status` endpoint in this repo -- if
there's a separate frontend for the live-view page, it'll need matching
changes to actually *render* more than one tab; I don't have that code.

Verified end-to-end against a real local browser (not fakes) in
`/tmp/test_multisite_delegation_live.py`: real tab isolation through the
actual shipped `_delegate_website_task` (two concurrent calls, each only
ever seeing its OWN page's content, verified past the "prefix could be
faked" concern by checking the browser-evaluated content specifically, not
just the label); real concurrency (lowering the cap to 1 measurably
serializes two calls that otherwise overlap); domain-allow blocking short-
circuiting before a semaphore slot is even taken; and the no-recursive-
delegation guard.

### 3. Real human-cursor, resolving the open question from before

The [cursor visualization](#cursor-visualization-the-open-question) section
above left this as an explicit trade-off I didn't want to resolve for you:
ghost-cursor/human-cursor-style tools need a raw Playwright `Page` object
we don't hold (we only talk to `@playwright/mcp` as a black-box MCP tool
server), so genuinely "real" cursor motion looked like it would mean either
forking `@playwright/mcp` or writing a custom driver. Asked directly, you
picked the real thing anyway. What actually made it possible without either
of those: a **second, independent CDP connection**, separate from
`@playwright/mcp`'s own, connected to the exact same browser. `human-cursor`
(the real npm package, `human-cursor@1.1.0` -- confirmed a *different*,
actively-published package from the `CloverLabsAI/human-cursor` GitHub repo
I'd initially looked at) needs no local browser binary of its own; it only
calls `chromium.connectOverCDP(...)`, so this doesn't touch the Docker
image's browser situation at all.

**New file, `messa/nodehelpers/cursor_driver.mjs`**: one small, long-lived
Node process **per deepsearch run, not per tab** -- started once by the
top-level `BrowserToolProvider` right after it has a Browserbase
`connectUrl`, handed down to (and shared by) every `delegate_website_task`
sub-worker, killed when the run ends. Talks JSON Lines over stdin/stdout
(no HTTP server, no extra port): `connect` once; `registerTab` per tab,
finding the right page by a content marker (`window.name`) rather than
creation order -- confirmed the hard way in a throwaway POC
(`/tmp/hc_poc/run_poc_multi.py`) that order-based matching has a real race
(pages can arrive out of creation order), while marker-based matching
doesn't; `move` (`cursor.moveTo({x, y})` -- deliberately only ever
*moves*, never clicks; the actual click/type still goes through
`@playwright/mcp`'s own tools, same separation of concerns the DOM-only
cursor always had); `unregisterTab`/`shutdown`.

**`BrowserToolProvider`**: after its tools are loaded (top-level and every
sub-worker alike), sets this tab's own marker via a raw, non-approval-gated
`browser_evaluate` call and registers it with the shared driver -- and
re-asserts the marker (fire-and-forget, so it never adds latency to the
agent's own navigate call) after every `browser_navigate`, since
`window.name` can reset on a cross-origin navigation. `_move_cursor_to`
still resolves the target element's on-screen coordinates via
`browser_evaluate` first (same target the real action is about to use,
same as before), but now sends the actual move to the Node driver instead
of calling `window.__messaCursor.moveTo()` in-page.

**`messa/assets/cursor_overlay.js`**, built on your customization rather
than replaced: added real `mousemove`/`mousedown`/`mouseup` listeners, so
the SVG arrow now follows the genuine dispatched events `human-cursor`
produces (snapped instantly per event, no CSS transition layered on top --
`human-cursor` already dispatches dozens of intermediate events per move,
confirmed at ~40-56 events for one on-screen move in testing, so the motion
is already smooth from the real event stream) instead of jumping between
two transitioned endpoints. Idle-breathing and the resting-spot drift you
built keep working exactly as before, unchanged, for the gaps where no real
events are arriving.

**`messa/package.json`** (the first one in this repo) pins
`playwright@1.55.0` and `human-cursor@1.1.0` -- the exact versions tested
throughout this work. **Dockerfile**: `npm install --prefix messa` runs
once at build time, before the `USER user` switch (same pattern as the
existing `pip install`) -- no `playwright install` needed, since
`cursor_driver.mjs` never launches its own browser.

**`config.DEEPSEARCH_HUMAN_CURSOR_DRIVER`** (default on) is a dedicated
rollback lever, separate from `DEEPSEARCH_CURSOR_OVERLAY` (which still just
means "draw a visible arrow at all"): if the real driver ever misbehaves on
a live deployment, flipping this to `false` falls straight back to the
original DOM-only CSS-transition cursor -- already proven in production --
without deleting or disabling either code path.

Verified against a real local headless Chromium throughout, formalizing the
`/tmp/hc_poc/` prototype into tests of the actual shipped files, not
reimplementations of their logic: `/tmp/test_cursor_driver_live.py` drives
the real `cursor_driver.mjs` directly over its real stdin/stdout protocol
(marker-based correlation even when tabs are registered in reverse order;
real dispatched `mousemove` events on the right page and only that page;
never a `mousedown`; clean shutdown). `/tmp/test_cursor_driver_wiring_live.py`
goes one level up, through a real `BrowserToolProvider` and an ordinary
guarded `browser_click` call -- exactly how the real agent loop triggers a
cursor move -- confirming real mousemove events reach the page both on the
top-level tab AND on a `delegate_website_task` sub-worker's own tab, through
the one shared driver instance.

**Caveat, same shape as every other one in this file:** everything above is
verified against a real local Chromium reached via a plain CDP debug port,
which is architecturally the same connection mechanism `--cdp-endpoint`
uses against Browserbase's real remote `connectUrl` -- but I don't have a
live Browserbase account in this sandbox, so the actual remote endpoint
itself is untested. I'd recommend one supervised real run of each new
feature (a multi-site task, a task that deliberately hits a login wall)
before trusting any of this unattended in production.

## Live view showed a blank page during multi-site delegation -- root cause and fix

Real feedback from a real run: multi-site delegation was genuinely
delegating (the log panel showed real sub-agent activity, navigation, the
works), but the live-view iframe itself just sat on a blank `about:blank`
page the whole time. Two screenshots confirmed it -- the address bar reading
`about:blank`, a blank white body, while the log underneath kept scrolling
with real steps. Also folded into this round: showing whichever tab is
actually active when several are running at once, and tightening
`delegate_website_task` sub-workers back down to genuinely small, one-site-
one-goal sessions per your explicit ask ("I dont want these sessions to go
longer").

### Root cause: `--isolated` moved real activity out of the context Browserbase's live view actually tracks

`self.live_view_url` is captured exactly once, in `__aenter__`, immediately
after `browserbase.create_session()` -- a single `GET /sessions/{id}/debug`
call against Browserbase's **default** browser context, before
`@playwright/mcp` even starts. The multi-site delegation work (previous
round) added `--isolated` to `@playwright/mcp`'s flags specifically to stop
two connections from colliding on the same page -- but `--isolated` does
that by giving each connection its own separate browser **context**, not
just its own tab. Once real work started happening inside that separate,
isolated context, the live-view URL captured before it ever existed had
nothing left to show -- Browserbase's own docs are explicit about this
("Always use the default context and page when possible to ensure proper
functionality of Verified features"). The log panel worked the whole time
because it's driven by `live_activity.py`, an in-memory Python dict, nothing
to do with Browserbase's own tracking -- which is exactly why the user could
see real steps scrolling by under a permanently blank video.

### Fix: claim a tab within the shared default context instead of a separate one

`--isolated` is removed from `_spawn_mcp_http_server`'s `mcp_args`
entirely. In its place, every `delegate_website_task` sub-worker calls
`@playwright/mcp`'s own `browser_tabs(action="new")` tool as the very first
thing it does on its fresh connection, in `BrowserToolProvider.__aenter__`,
before any guarded navigation happens. This claims a genuinely separate tab
for that connection -- same isolation guarantee `--isolated` gave (verified
empirically in a throwaway probe, `/tmp/probe_tabs_new.py`: two concurrent
connections each landed on their own tab, zero cross-talk, zero
navigation-interruption errors) -- but *within* Browserbase's one shared
default context, which is the context its live-view/debug-URL tracking
actually follows end-to-end. The top-level provider needs no change at all:
it never shares its connection with anyone, so it simply keeps using
Browserbase's original default page, exactly as it did before multi-site
delegation existed. Re-verified against the real shipped code with a real
local Chromium (`/tmp/test_multisite_delegation_live.py`,
`/tmp/test_cursor_driver_wiring_live.py`) -- both still pass unchanged,
confirming tab isolation and per-tab cursor movement work the same way
without `--isolated`, and the MCP server's own "Open tabs" listing in the
test output now shows every tab living inside one shared context (something
`--isolated` would never have allowed).

### Follow whichever tab is actually active

The video itself was still stuck on a single fixed URL even setting the
root cause aside -- the user's other ask was "when multiple agents are
actively using the page just show whichever one." `live_activity.py` gained
three more per-user fields: `bb_session_id` (the raw Browserbase session id,
set once by the owning provider right after it opens the session --
`live_view_url` alone was never enough to re-query anything), `active_tab_id`
(`None` for the top-level tab, or a sub-worker's own `tab_id` -- updated on
**every** guarded tool call, top-level and sub-worker alike, so it always
names whichever tab most recently did something), and `url` (the top-level
tab's own current url, mirroring the `url` field each `tabs` entry already
carried). A new `channels/browserbase.get_session_pages(session_id)` wraps
the same `/debug` endpoint `get_live_view_url` already used, returning its
`pages[]` array instead of the session-level url.

`server.py`'s `/live/{token}/status` route now calls a new
`_resolve_active_tab_live_view_url` helper: when the active tab is a
sub-worker (not the top-level tab), it fetches `pages[]`, matches by url
against that tab's own recorded url, and substitutes that page's own
`debuggerFullscreenUrl` in place of the DB's fixed session-level one --
falling back to the original url whenever there's nothing to resolve yet
(no active sub-worker, no session id, no matching page, or the Browserbase
call itself fails). No frontend change was needed for this: I went and
actually read `live_view_page.py`'s `ensureStage` function before assuming
one was required, and it already rebuilds the whole stage (including a
fresh iframe `.src`) whenever `live_view_url` differs from what's currently
shown -- it just never *received* a changing url before now. Verified with
a dedicated new test, `/tmp/test_active_tab_live_view.py`: unit-level checks
of the resolution logic itself (no override while the top-level tab is
active, graceful fallback on a missing session id/no match/a failing
Browserbase call), plus two integration-level runs against a real local
Chromium proving `bb_session_id`/`active_tab_id`/`url` are genuinely
populated by the real `__aenter__`/`guarded()` code paths (not a hand-built
fixture) and resolve to the right per-tab debug url end to end.

### Sub-workers, tightened back down to one site, one goal, no wandering

Explicit ask: "a sub-agent should receive one website and one goal thats
all, I dont want these sessions to go longer, Its just what deepsearch was
doing sequentially, I wanted to multi process it with sub-agents so the
process will get faster." `DEEPSEARCH_SUBAGENT_MAX_STEPS` drops from `15` to
`8` -- enough for navigate, snapshot, a couple of actions, a final read, not
a multi-page exploration. `_SUBAGENT_SYSTEM_PROMPT` gained an explicit line
telling the sub-worker to do exactly what it was asked and nothing more --
no browsing to other pages or products, no going looking for extra things to
report -- and to stop and reply the moment its one goal is done rather than
spending its (now much smaller) step budget wandering. `DEEPSEARCH_SYSTEM_PROMPT`
(the top-level orchestrator's own prompt) gained a matching line telling it
to keep each `delegate_website_task` call's `instructions` short and
self-contained -- one simple goal per call, not several unrelated sub-goals
bundled together -- reinforcing that this feature is meant to be the same
per-site work deepsearch always did sequentially, just parallelized, not a
new or more elaborate kind of task.

## Live view redesigned as a multi-tile grid, plus a real 5-minute session cutoff bug

More real feedback from a real run: the cursor and multi-agent delegation
themselves were confirmed working well, but the live view was *still*
showing a blank page in your screenshots even after the previous round's
`--isolated` fix -- because that fix made ONE tab visible (whichever was
"active"), and your screenshots happened to be taken while a *different*
tab than the one being followed was the one doing something. Your own
proposed fix was better than mine: don't follow one tab at a time at all --
show every open tab at once, the way Browserbase's own dashboard does (one
live-view link per tab, per
https://docs.browserbase.com/platform/browser/observability/session-live-view).
Also reported: browser sessions cutting off at the 5-minute mark
*entirely*, not scoped to one stuck tab; and a request to make the live page
itself production-quality.

### Root cause of the 5-minute cutoff: never our own code, a Browserbase project setting

`DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS` is also 300 seconds (5 minutes),
so it looked like the obvious suspect -- but reading through
`request_human_help`, it only ever gives up on the ONE tab that's waiting
and returns a `BLOCKED` string; it never closes the browser or ends the run.
`DEEPSEARCH_MAX_SESSION_SECONDS` (1200s = 20 minutes) is the actual overall
session cap, wrapped around both the top-level agent's own run and each
`delegate_website_task` call. Neither of those is 5 minutes. The real cause,
confirmed against Browserbase's own API docs: `browserbase.create_session`
never passed a `timeout` in its request body, so every session silently
inherited "the Project's `defaultTimeout`" -- a dashboard setting outside
this repo entirely, evidently defaulting to 5 minutes on this project. That
explains the exact symptom reported ("the whole browser session is cutting
off at 5 min mark closing the entire task") regardless of whether a task
ever hit a login wall at all. Fix: `config.BROWSERBASE_SESSION_TIMEOUT_SECONDS`
(default 1800s = 30 minutes, comfortably above our own 1200s cap so our own
code is what actually bounds a session day to day) is now passed explicitly
on every `create_session` call -- no more dependency on an out-of-repo
dashboard default.

### The live view is now a grid: one tile per open tab, added and removed live

`server.py`'s `/live/{token}/status` route no longer resolves a single
"currently active" `live_view_url` (the previous round's approach) -- it now
returns a `tiles` array, one entry per currently-open browser tab (the
top-level orchestrator's own tab plus every live `delegate_website_task`
sub-worker), each carrying its own `heading`, `description`, `steps`,
`waiting_for_human`, and its own resolved `live_view_url`. A new
`_build_live_tiles` helper makes exactly ONE `browserbase.get_session_pages`
call per poll (not one per tile) and matches every tab's own last-known url
against that single `pages[]` result -- a tab whose url has no match yet
(Browserbase's `pages[]` isn't push-live; a just-opened tab can take a
moment to show up) gets `live_view_url: null` rather than a stale or wrong
url, and the page shows a "Connecting..." placeholder for that tile until a
later poll resolves it. The top tile's heading is the task title Messa
already sets (`live_view_task`); a sub-worker's tile heading is the
hostname it's working on (`booking.com`, not the full url) via a small
`_tile_heading` helper. The `active_tab_id` tracking added last round
(originally meant to pick ONE tab to follow) wasn't thrown away -- it's now
used to put a subtle accent ring on whichever tile is *currently* the busiest
one, a nice-to-have on top of showing all of them rather than the whole
mechanism.

`live_view_page.py` is a substantial rewrite: a responsive CSS grid
(`repeat(auto-fit, minmax(380px, 460px))`, centered) instead of one fixed
card, each tile built and torn down by JS as tabs open and close (a
`tile-enter`/`tile-exit` class pair driven by `requestAnimationFrame` gives
new tiles a soft fade-and-settle-in and closed ones a fade-out, instead of
the grid jumping under your eyes every 4-second poll). Each tile is
deliberately small: a heading row (with a "Waiting for you" pill when that
tab's `request_human_help` is active), one line of current description, the
video, and a short log capped to `TILE_LOG_LINES = 4` most-recent lines --
per your explicit ask ("bottom log updates maybe max of 3-5 lines"). The old
per-tile-height `fitVideo()` JS hack (needed because a pure CSS
`aspect-ratio` used to fight with the single big card's own fixed
`max-height`) is gone entirely -- a grid tile just sizes itself, so a plain
CSS `aspect-ratio: 1280/800` on the video area does the job with no JS
involved.

Design itself: rebuilt the "polished" skin from scratch as the actual
production-facing one this time -- a light, neutral, San-Francisco-font
look (`--bg: #f5f5f7`, white cards, a single blue accent, soft shadows
instead of hard borders, `backdrop-filter: blur` on the sticky header) aimed
squarely at "apple like website that is clean but elegant." Full dark-mode
variables alongside it. The "terminal" skin (black/monospace/gritty) still
works -- same markup, same JS, just its existing CSS variables -- for anyone
with an old `?style=terminal` link bookmarked, but it wasn't where this
round's design effort went; the docstring at the top of `live_view_page.py`
says so explicitly.

Verified with a real headless Chromium rendering the actual shipped page
(not a mockup) against a mocked `/status` endpoint: multiple tiles render
with zero JS console errors; a tile with no `live_view_url` yet shows the
"Connecting..." placeholder correctly (an early version of this had a bug
here -- comparing `null === null` on first render skipped ever drawing the
placeholder at all, since a genuinely-still-connecting tab and "nothing
rendered yet" both started out `null`; fixed by tracking a tile's rendered
state with `undefined`, which never equals `null`, instead); a tile closing
and a new one opening mid-poll animates correctly and lands the new tile in
the freed grid slot; both the light and dark palettes and both skins render
cleanly. On the backend, a new `/tmp/test_live_tiles.py` covers
`_tile_heading` (plain hostnames, `data:`/`about:blank` urls, `None`),
`_build_live_tiles` (url resolution with a match / no match yet / no
session id / a failing Browserbase call; heading computation; the `active`
flag), and the full `/live/<token>/status` route end-to-end through
FastAPI's `TestClient` with real `live_activity` state populated the same
way the real guarded()/`_delegate_website_task` code paths would. It
supersedes the previous round's `/tmp/test_active_tab_live_view.py`, whose
`server._resolve_active_tab_live_view_url` no longer exists (replaced by
`_build_live_tiles`) -- that old file is left in place for the record but
would fail if re-run as-is.

**Caveat, same shape as every other one in this file:** the grid and tile
resolution logic is verified against a real headless Chromium and a real
FastAPI route with mocked Browserbase responses, not a live Browserbase
account -- I don't have one in this sandbox. The one thing I'd specifically
want watched on a real multi-site run: how quickly a newly-opened
sub-worker tab's `pages[]` entry actually appears (governs how long its
tile sits on "Connecting..." before the real video shows up), since that's
paced entirely by Browserbase's own indexing, not anything in this code.

## Live view tile count was wrong (real bug), and reverting to the terminal skin as default

Real feedback from a real 4-tab run: our tile grid rendered only **1** tile
even though Browserbase's own debugger showed 4 real tabs open in the
session. Also: the "polished" redesign from the previous round didn't land
well visually ("I did not like it to be honest, lets just switch to our
terminal style for now, we can work on the UI later").

### The undercount bug: tile existence was tied to OUR bookkeeping, not the real browser

`_build_live_tiles` (previous revision) built one tile for the top-level tab
plus one per `live_activity`'s own `tabs` dict entry -- which only ever
gets an entry when a tab is opened through `delegate_website_task`. But a
tab can exist without ever going through that path: the top-level
connection's own model has `browser_tabs` on its toolset like every other
`@playwright/mcp` tool (nothing in this codebase hides it), and a click on a
`target="_blank"` link opens a new tab on its own too -- neither is tracked
by our delegation bookkeeping at all, so neither ever got a tile, no matter
how long they stayed open. That's exactly what the 4-tab test caught: 2 of
those 4 tabs weren't sub-workers our code knew about.

Fix: `_build_live_tiles` now derives tile *existence* straight from
Browserbase's own `pages[]` (`channels/browserbase.get_session_pages`) --
whatever tabs are really open, for whatever reason, get a tile. Each real
page's own `debuggerFullscreenUrl` becomes that tile's video directly (no
url-matching needed for that part anymore, which also removes a whole class
of "no match yet -> stuck on Connecting..." failure the previous revision
could hit). `live_activity`'s own tracked state (the top-level's
description/steps, each `delegate_website_task` sub-worker's own log) is
now used only as *best-effort enrichment* -- matched against a real page by
url, filling in a nicer heading and the running log when we happen to have
one for it. A page with no match still gets a perfectly good tile, just
with a plain hostname heading and an empty log instead of enriched ones,
rather than not existing at all. Tiles are sorted by each page's own
(stable-for-its-lifetime) id so the grid doesn't visibly reshuffle every
4-second poll. Covered by a rewritten `/tmp/test_live_tiles.py`, including a
scenario that specifically reproduces the reported bug (4 real pages, only
2 known to our own tracking) and asserts all 4 still get tiles.

### Back to the terminal skin as the default

`live_view_page.py`'s `LIVE_VIEW_STYLE` is back to `"terminal"`. This is a
skin-only reversion -- the underlying multi-tile grid mechanism from the
previous round (one tile per tab, added/removed live, short per-tile log)
is unchanged and is what the terminal skin now renders; only the CSS
variables (colors/fonts/shapes) reverted. The "polished" skin from last
round still exists and still works (`?style=polished`), just isn't the
default until that design gets revisited later, per your own framing
("we can work on the UI later").

### Model note

You mentioned Messa's main agent is currently configured on
`deepseek/deepseek-v4-pro-0813` -- noted for context; nothing in this round
depended on which model that env var points at, so no code change was
needed for it.

## Live view went fully blank mid-task, plus disconnect/navbar/scrollbar styling

Real feedback from a real run: the live page showed a **completely blank
screen** with zero tiles while 5 real tabs were open and working in
Browserbase's own dashboard -- not a wrong-tab bug this time, an empty grid.
You'd already tried swapping which URL field `_build_live_tiles` read
(`debuggerFullscreenUrl or status.get('live_view_url')` instead of
`debuggerFullscreenUrl or page.get('debuggerUrl')`) and it didn't help,
which was actually a useful data point: it ruled out "wrong field" as the
cause before I ever looked at the code.

### Root cause: no fallback when the per-page pipeline produced nothing

`_build_live_tiles` builds its tiles entirely from
`channels.browserbase.get_session_pages(bb_session_id)` -- a live call to
Browserbase's `/sessions/{id}/debug` endpoint. The previous revision had
exactly one path through that function that returned `[]`: whenever that
call produced no usable pages, for *any* reason -- no `bb_session_id` yet
recorded, the API call itself failing (network hiccup, rate limit, session
mid-teardown), or a genuinely empty `pages[]` for a moment. An empty list
of tiles is indistinguishable from "nothing running" to the frontend, so
the grid rendered as blank -- even though `status.get('live_view_url')`
(the session-level URL, always present once a Browserbase session exists)
was sitting right there unused. Your own fix attempt couldn't have changed
this, because it only changed *which field wins when both are present* --
this bug is about the case where the whole per-page pipeline hands back
nothing at all, upstream of that `or`.

Fixed by giving `_build_live_tiles` a real fallback: when
`get_session_pages` yields no pages for any reason, it now builds **one**
tile from `status['live_view_url']` (the same field the very first,
pre-tile-grid version of this page always showed) instead of returning
`[]`. Only if that's *also* missing -- meaning there's genuinely no session
at all -- does it fall through to an empty grid. Verified two ways: a unit
test that directly reproduces the reported scenario (`active=True`, a real
session-level url, `get_session_pages` raising) and asserts the response
always has at least one tile; and the full `/live/<token>/status` route
through FastAPI's `TestClient`, same assertion, end-to-end.

### Styling and disconnect handling, per Browserbase's own docs

You linked Browserbase's live-view docs page and asked me to apply its
disconnect-handling and styling guidance. Three changes, all sourced
directly from that page:

- **`navbar=false`.** The only documented styling query param -- hides
  Browserbase's own embedded chrome (URL bar, tab strip) inside the
  iframe. Applied in one place, `_hide_navbar()`, to every `live_view_url`
  the route hands back (per-page and the new fallback tile both), so every
  tile gets it automatically rather than each call site remembering to add
  it.
- **`sandbox="allow-same-origin allow-scripts"`** on every tile's
  `<iframe>` -- the exact attribute shape from Browserbase's own embedding
  example in that doc.
- **Graceful disconnects.** Per the docs, Browserbase's live-view iframe
  posts `window.postMessage("browserbase-disconnected", ...)` to its parent
  when a session or tab goes away, and otherwise renders its own raw
  "could not connect" page inside the iframe -- exactly the "error loading"
  screen you said not to show. `live_view_page.py` now has one page-level
  `message` listener that matches the event's `source` window against
  whichever tile's iframe it actually came from (so a disconnect on tile 2
  can never affect tiles 1 or 3), and swaps *only that tile* to a calm,
  blank, unmistakably-Messa placeholder (a small dark square, no text, no
  red/alarm styling) instead of letting Browserbase's own error UI show
  through. Verified with a real headless-Chromium test: 4 tiles rendering
  simultaneously (matching your actual 4-5 tab scale), each pointed at its
  own fake page; triggering a disconnect on just one iframe swapped only
  that tile to the blank placeholder while the other 3 kept rendering their
  own distinct content, completely unaffected -- screenshots confirm this
  visually (before/after), not just the assertions.

There's no documented query param for hiding scrollbars (only `navbar` is
documented), so per the docs' own suggestion that page-level styling goes
through `page.evaluate()`: `cursor_overlay.js` (already injected into every
browsed page via `@playwright/mcp`'s `--init-script`) now also injects a
tiny `<style>` tag hiding the scrollbar (`scrollbar-width: none` +
`::-webkit-scrollbar { display: none }`) the first time it runs on a page,
independent of its existing cursor-overlay guard so the two don't interfere
with each other.

### Verification

Re-ran the full regression suite most relevant to what changed this round
(`/tmp/test_live_tiles.py`, `/tmp/test_cursor_overlay.py`,
`/tmp/test_multisite_delegation_live.py`,
`/tmp/test_cursor_driver_wiring_live.py`, plus the human-help/pause-loop/
live-link suites) -- all still green, nothing regressed. New:
`/tmp/test_live_multitile_sim.py`, a real headless-Chromium test at your
actual reported scale (4 simultaneous tiles, not 2) confirming all 4 load
with the right `navbar=false`/`sandbox` attributes, all 4 render distinct
content at once with no cross-talk, and a disconnect on one tile leaves the
other 3 fully intact.

## Still only 1 tile for 5 open tabs -- a real ordering bug in when live_activity.start() ran

Right after the fix above shipped, you reported the exact same "only 1
tile" symptom on a run with 5 real tabs open in Browserbase's own
dashboard. Worth being precise about what this was NOT: `live_view_page.py`
already had the full tile-grid rendering (`buildTile`/`renderGrid`, looping
over `data.tiles`) from an earlier round, not a single static `<iframe>` --
so this wasn't a leftover old frontend. The bug was entirely server-side,
in exactly which tiles `/live/<token>/status` was handing that (correct)
frontend to render.

### Root cause: `live_activity.start()` was silently wiping `bb_session_id` right after it got set

`_build_live_tiles` (server.py) needs `activity["bb_session_id"]` to call
`browserbase.get_session_pages()` and build one tile per real open tab; with
no `bb_session_id`, it falls back to exactly one tile built from the
session-level `live_view_url` -- by design, as the "never go blank" safety
net from the previous fix. That fallback path was firing on *every* run,
not just the rare failure case it was meant for.

The actual sequence in `tools/deepsearch_tools.py`'s `_run()`:
1. `BrowserToolProvider.__aenter__` creates the Browserbase session and
   immediately calls `live_activity.set_session_id(user_id, bb_session_id)`
   -- writing `bb_session_id` into that user's `live_activity` entry.
2. `__aenter__` returns.
3. *Then*, back in `_run`, `live_activity.start(user_id, task_title)` used
   to run -- and `start()`'s whole job (see its own docstring) is to
   replace that user's entire `live_activity` entry with a brand-new dict,
   as the correct, intentional reset for a fresh delegation. It doesn't
   selectively clear fields; it just wasn't expected to run *after*
   something had already written into that entry for the same run.

So `bb_session_id` got set, then immediately clobbered back to `None`
moments later, on 100% of runs -- not an intermittent Browserbase API
hiccup like the previous round's fallback was built for. `_build_live_tiles`
therefore always hit its fallback path and always rendered exactly one
tile, regardless of how many tabs were genuinely open. This also explains
why your own earlier attempt (swapping which URL field wins,
`debuggerFullscreenUrl or status.get('live_view_url')`) never helped: that
line of code was never being reached at all, since `pages` was never even
fetched without a `bb_session_id`.

### Fix: start the log before opening the browser, not after

Moved the `live_activity.start(user.user_id, task_title)` call to right
before `async with BrowserToolProvider(...)`, instead of inside that block
after `__aenter__` returns. `task_title` is already known at that point in
both the fresh-task and resumed-session branches, so this needed no other
change. Now `start()` creates the entry first; `set_session_id()` (called
from inside `__aenter__`, moments later) uses `_state.setdefault(...)`, so
it lands on that same existing entry instead of a later `start()` call
wiping it out from under it.

Verified two ways: a focused test proves the exact call-order regression
directly (`start()` then `set_session_id()` preserves `bb_session_id`;
the old `set_session_id()` then `start()` order reproduces the bug,
`bb_session_id` comes back `None`) -- appended to `/tmp/test_live_tiles.py`
so a future refactor that reintroduces this ordering gets caught
immediately; and the full existing regression suite (multi-site delegation,
cursor driver wiring, pause loop, live-link tests) re-run clean, confirming
this reorder didn't disturb anything else that touches `live_activity`
during the same window.

## Tiles stable, but no live logs and no visible cursor -- two more real bugs

Once tiles stopped flickering, you flagged two things left: each tile's
description/log wasn't updating even though real work was happening, and
the cursor never appeared on any tab despite actions clearly executing.
Both were real, separate bugs, not the same root cause -- and the cursor
one uncovered something bigger than just the cursor.

### Missing live logs: enrichment matched by EXACT url, which routinely doesn't match

`_build_live_tiles` builds one tile per real Browserbase page, then
enriches it with our own tracked description/step log by looking up that
page's url in a dict keyed by whatever url we last told `browser_navigate`
to go to. That's fragile: what we navigated to and what Browserbase's
`pages[]` later reports as that tab's real, current url routinely differ
on a redirect (`kayak.com` -> `https://www.kayak.com/`), an added query
string, or a trailing slash -- any mismatch meant the lookup silently
missed and that tile's log just never populated, even while the tab was
genuinely active.

Fixed with a hostname-based fallback match (`_host_key`, tried after an
exact url match fails) -- reliable specifically because of this app's own
design: `DEEPSEARCH_SYSTEM_PROMPT` enforces one website per
`delegate_website_task` call, so in ordinary use every concurrently open
tab is already on a distinct host, making hostname matching essentially as
unambiguous as a working url match would have been. Verified with a test
that deliberately gives the fake Browserbase pages redirected/www/query-
string-drifted urls and confirms both the top-level tile and a sub-worker
tile still show their real, live description and steps.

### Cursor invisible: `--init-script` never fires in `--cdp-endpoint` mode -- confirmed locally, root-caused, and fixed

This one had a much bigger real cause than "the cursor is broken":
`@playwright/mcp`'s `--init-script` flag -- the mechanism the whole cursor
overlay AND the reading-animation feature depend on to get
`cursor_overlay.js` onto a page at all -- silently never runs when
`@playwright/mcp` connects to an already-running browser via
`--cdp-endpoint` (deepsearch's real mode against Browserbase). It only
works when `@playwright/mcp` launches its own local browser, a different
internal code path. This wasn't a guess: reproduced directly against a
real local Chromium with `@playwright/mcp`'s actual package, both for the
tab that already existed at connect time and for a brand-new tab opened
afterward -- `window.__messaCursor` came back `"undefined"` on both, every
time. Meaning: the cursor overlay (and reading-animation) has likely never
actually worked against a real Browserbase session, regardless of which
cursor mode was configured -- this was the first time it got tested against
the real thing end-to-end.

The fix doesn't route through `--init-script` or `context.addInitScript()`
at all (a follow-up local test showed `addInitScript` registered from one
independent CDP connection also doesn't reliably reach a page a *different*
independent connection is the one navigating -- the exact shape of
`cursor_driver.mjs` vs. `@playwright/mcp`, two separate connections to the
same Browserbase session). Instead, `cursor_driver.mjs`'s `registerTab`
handler now directly `page.evaluate()`s `cursor_overlay.js`'s source onto
the exact page it just found, the moment it finds it -- an immediate
execution in a document that already exists, not a "run before future
scripts" registration, which a follow-up local test confirmed works
reliably regardless of which connection later drives that page.
`registerTab` already runs once when a tab opens and again after every
`browser_navigate` (`_assert_cursor_marker`), so this same call re-mounts
the overlay after a real navigation wipes it, with no new call site needed.
`--init-script` stays in `_spawn_mcp_http_server` too, best-effort, for the
one gap this doesn't cover (`DEEPSEARCH_CURSOR_OVERLAY` on with
`DEEPSEARCH_HUMAN_CURSOR_DRIVER` off, so no `cursor_driver.mjs` running at
all).

Verified end-to-end against the real `cursor_driver.mjs` process (its
actual JSON-lines protocol, not a stand-in) driving a page navigated
entirely by a *separate* connection: the overlay mounts on first
`registerTab`, survives a real `move` command actually moving the arrow,
and correctly re-mounts after a real navigation wipes the page and
`registerTab` runs again -- matching exactly how Python re-asserts the
cursor marker after every `browser_navigate` today.

### Cursor size

Also fixed while in there: the arrow's scale was `2.7` (`2.1` mid-click) on
a 56x56 base SVG -- about 151px on the real 1280x800 browser viewport,
nearly 12% of its width. Now `0.65`/`0.5` -- about 36px, clearly visible
without dominating the screen. Confirmed with a rendered screenshot.

## Live view gets a second page: reminders, this week's schedule, tasks, projects, contacts

Once the live browsing grid + cursor were working end-to-end, you asked for
more on the same `/live/<token>` link: "I want to show them the Reminders,
Schedule as a weeks chart..., Tasks, Projects, contacts, etc. All these on
one page in different tiles. The browser windows still stay on the first
page and second page we can keep these and add a toggle on the top to
switch between them." That's a second, independent page on the exact same
permanent link, not a new page or a new share mechanism -- so it reuses
`users.live_share_token` (migration 006) end to end, resolved through a new
`db.get_user_by_live_token` rather than `get_live_status_by_token` (which
is scoped to browsing state -- `active`/`live_view_url`/`task` -- and used
by a different route; keeping them separate meant neither caller has to
reason about fields it doesn't need).

**Backend:** a new `GET /live/<token>/dashboard` route in `server.py`
gathers `tasks`, `pending` `reminders`, `active` `projects`, `people`
(contacts), and this week's `calendar_events` concurrently via
`asyncio.gather`, using Messa's existing Phase 1 data model unchanged --
nothing new to migrate for the data itself. "This week" is computed in the
user's *own* stored timezone (`_week_bounds_utc`, same `ZoneInfo` pattern
`timeutil.py` already uses elsewhere), Monday 00:00 through the following
Monday, then handed to a new `db.list_calendar_events_for_range` as a
tz-aware UTC pair -- doing this in UTC directly would be wrong (a Monday
morning event could land in "last week" for a user west of UTC). Each
event's start time then buckets it into one of the 7 returned `days`, and
times are formatted with a new `_format_time_local`/`_format_due_local`
pair (`"9:00 AM"`, `"Aug 30"`, `"Aug 30, 3:00 PM"` when a real time-of-day
is set, nothing when a task has no due date at all). 404 for an unknown
token, same "a bad link never quietly looks like a valid empty state" rule
`/live/<token>/status` already follows.

**Frontend (`live_view_page.py`):** the browsing grid you already had
(`#main`) and the new dashboard (`#page-dashboard`) are both permanent
sibling elements now, toggled with a header nav (`Live browsing` /
`Dashboard`) that only ever flips the `hidden` attribute -- switching pages
never rebuilds either one's DOM. That matters specifically because of how
carefully the browsing grid avoids re-writing an iframe's `src` (see "Tiles
stable..." above) -- a naive `innerHTML` swap on toggle would have thrown
that away and reintroduced the white-flash bug the moment someone switched
pages during a live run. The dashboard polls its own endpoint separately
from the 4s browsing poll, on a much slower 30s cadence (tasks/reminders/
schedule don't change second to second the way a live tab does).

The schedule renders exactly as specced -- a heading with the date range
and year, then one native `<details>`/`<summary>` disclosure per day, time
on the left of each event and the title (+ location, when there is one) on
the right, today's day open by default. Tasks/reminders/projects/contacts
render as smaller cards below it. Because the whole dashboard re-renders
on every poll (simplest correct approach for data this size), a day the
user opened or closed by hand would otherwise silently reset every 30
seconds -- fixed with a small `openDays` map keyed by date that survives
across renders, so a manual override sticks until the user changes it
again. Both new cards reuse the exact same `--card-bg`/`--border`/
`--radius`/`--shadow`/etc. CSS variables the browsing tiles already define
per skin, so "terminal" and "polished" both render correctly with zero
skin-specific dashboard code.

Verified with a real-headless-Chromium test (`test_dashboard_toggle.py`,
following the same pattern as the multi-tile grid tests): default page on
load is browsing with the dashboard hidden; toggling in renders all 5
tiles with correct content, including an empty list showing its own empty
state rather than a blank card; a day opened or closed by hand survives a
full dashboard re-render; and four full round trips between pages never
reload the browsing tile's iframe (checked via the same load-counter
technique as the reload-bug regression test). The route/data-shaping logic
itself has its own suite (`test_dashboard_route.py`) covering the week-
boundary math across timezones (including a same-instant-different-local-
day case between New York and Tokyo), the date-label formatting, and the
full JSON shape through FastAPI's `TestClient`.

## Default morning + evening briefings for every user

You already had morning briefings set up for existing users (rows in
`cron_jobs`, created the ordinary way -- asking Messa, who staged it via
`propose_create_recurring_cron` for you to confirm). You asked for two
things: make an evening briefing exist for those same users too, and make
both automatic going forward -- every new user gets both by default, no one
has to think to ask.

**Why this couldn't just be "add one row to the database":** there's no
direct line into your live Neon DB from here, and even if there were, a
one-time INSERT wouldn't cover new users signing up afterward. So instead
of a migration that touches data, this is application code that provisions
both jobs itself, the moment it's needed:

`db.ensure_default_briefings` (new) checks whether a user already has a
`morning_briefing`/`evening_briefing` cron job (tagged via a new nullable
`cron_jobs.kind` column, `migrations/011_cron_job_kind.sql` -- NULL for
every ordinary user-created job, untouched) and creates whichever one is
missing. It's called from `cli.load_user_context` right after
`ensure_timezone_resolved` -- the exact same "self-heals on every load"
spot that function already uses to backfill timezone resolution for
accounts older than that feature. That one hook covers every path a user
ever gets loaded through: a brand-new signup's very first turn, an existing
user's next inbound text, and `_production_cron_loop` loading a user to run
one of their *existing* due jobs (their morning briefing firing tomorrow at
7am is itself what backfills their evening one). No manual backfill script
needed, and no direct production DB access required from this end.

This deliberately bypasses the `propose_create_recurring_cron`/
`pending_actions` confirmation gate that a model-initiated cron job goes
through -- that gate exists for a model deciding, on its own, to create new
standing automation; this is the product itself turning on a default for
everyone, the same category of thing as `get_or_create_live_share_token`
silently issuing a live-view link with no confirmation step. Existence of a
row with that `kind` -- regardless of its status -- is what's checked, so a
user who later pauses or cancels their evening briefing stays off; this
never resurrects it.

**Timing:** both fire at fixed default local times -- 7:00 AM and 8:00 PM,
in whatever timezone is actually stored on the user's row at the moment
each is provisioned. A user who hasn't confirmed a real timezone yet gets
provisioned against `config.DEFAULT_TIMEZONE` as a placeholder; the same
function self-heals that too -- once `ensure_timezone_resolved` confirms a
real timezone on a later call, `ensure_default_briefings` notices the
briefing rows' stored zone no longer matches and corrects both the zone and
`next_run_at`, so "7am" ends up meaning 7am where the user actually lives,
not wherever the placeholder pointed. It deliberately never touches an
*unconfirmed* placeholder-to-placeholder match, so nothing is "corrected"
onto a timezone nobody's actually vouched for yet.

**Content** is entirely prompt-driven, not a template: both jobs' stored
`prompt_or_task` (in the new `config.DEFAULT_BRIEFINGS` dict) is fired
through the exact same `load_user_context` -> `build_orchestrator` ->
`run_message` path a real inbound text takes (see
`server.py`'s `_production_cron_loop`, already covered above) -- there's no
separate "briefing renderer" to keep in sync with Messa's own tools. The
morning prompt asks for today's calendar events and tasks due/overdue, a
short one-line weather lookup (via Messa's own `web_search` tool -- no new
integration, no API key), and, if there's nothing scheduled and nothing
due, a nudge that she can help set something up or take care of anything on
their mind, including things that need clicking around a real website
(deepsearch). The evening prompt asks for what got done today, plus a
preview of tomorrow's schedule and anything due tomorrow or still overdue,
with the same "quiet day" nudge if there's nothing to report.

### A real bug caught before it shipped: this would have double-texted your 3 existing users

You already had a simple, hand-set-up 6am morning briefing for 3 test
users -- created the ordinary way, by asking Messa, so those rows'
`kind` is NULL (only this new provisioning path ever sets it). The first
version of `ensure_default_briefings` checked for a row tagged
`kind = 'morning_briefing'` before creating one -- which that existing 6am
job, being untagged, would never satisfy. Left as written, every one of
those 3 users would have ended up with TWO morning briefings: their
original 6am one and a brand new 7am one, forever. You caught this by
asking what happens to the existing 6am jobs before it ran anywhere.

Per your call ("replace entirely"), `_retire_legacy_briefing_if_any` now
runs before every insert: it looks for exactly one untagged
(`kind IS NULL`, not already cancelled) cron job whose own `prompt_or_task`
text contains "morning brief"/"evening brief" (`config.
LEGACY_BRIEFING_MATCH_KEYWORDS`), cancels it, and only then creates the new
tagged row with the fuller content at the new default time. Deliberately
conservative on the match itself: zero candidates or more than one and it
does nothing (leaves every existing job alone, still creates the new one
normally) rather than risk cancelling a real user's real automation on a
guess -- a false negative just means one recoverable duplicate; a false
positive means silently deleting something they set up on purpose.

Verified with a hand-built fake asyncpg pool (`test_default_briefings.py`,
since this is real SQL-shaping logic worth exercising directly rather than
mocking away): a brand-new user gets exactly two rows inserted, tagged and
timed correctly; a user who already has both gets zero duplicate inserts,
including when one of those rows is a cancelled job (existence, not
status, is what's checked); the timezone self-heal fires only once a
timezone is actually confirmed, never while it's still an unverified
placeholder; the legacy-replacement path cancels the one clear match and
creates the new row with the fuller content in its place, while an
ambiguous (2+ candidate) match or a genuinely unrelated existing job (a
weekly check-in, say) is never touched; and the whole function is a clean
no-op if migration 011 hasn't been applied yet. A separate direct test
confirms `load_user_context` calls `ensure_default_briefings` with the
already-timezone-resolved row, in the right order.

**Known trade-off, not fixed here:** a user who signs up but never finishes
onboarding (never gives a name/city) still gets both jobs provisioned
against the placeholder timezone right away, per "every user, new or
existing" as asked -- meaning it's possible for a half-onboarded account to
get a 7am-somewhere-else-in-the-world text before they've said anything
back. If that turns out to feel premature in practice, the fix is a one-line
gate in `ensure_default_briefings` on `user_row["onboarding_step"] ==
"complete"` -- deliberately left out for now rather than guessed at.

## Cursor going invisible mid-run -- a real race in the post-navigate re-assertion

You reported the cursor showing at the start of a run, then going invisible
inside a tab once a website finished loading -- browsing itself kept
working fine (real clicks, real reads), just the drawn arrow vanished.

Root cause: `_assert_cursor_marker` (which re-injects `cursor_overlay.js`
onto the tab after every `browser_navigate` -- a real navigation wipes all
page JS state, overlay included) was fired via `asyncio.create_task(...)`
and never awaited, on the theory that a cursor-driver round trip shouldn't
add latency to the navigate it's piggybacking on. In practice this raced:
the model's very next action (almost always `browser_snapshot`, immediately
followed by real clicks/types) never waited on that background task, so if
re-injection hadn't finished by the time those next actions ran, the
overlay was simply never mounted on that page at all -- not "delayed," just
absent for good until the *next* navigation gave the race another chance to
go either way. This matches the report exactly: human-cursor's real mouse
events don't depend on `cursor_overlay.js` being present (only the drawn
SVG arrow does), so the actual clicking/typing/finding never had any reason
to be affected.

The honest fix is to await it -- but the old `registerTab` handler in
`cursor_driver.mjs` made that expensive: every call, first-time or repeat,
re-polled `context.pages()` for a page matching `window.name` (up to 8
seconds in the worst case), even though a re-navigate's Page object is the
exact same instance Playwright already handed back the first time a tab
opened. `registerTab` now has a fast path -- if the marker is already
tracked, it reuses the cached Page reference directly and skips the poll
entirely, turning re-registration into one `evaluate()` call. That's what
makes it safe to await inline: `deepsearch_tools.py`'s `guarded()` now
`await`s `_assert_cursor_marker()` after every navigate instead of firing
it detached, so the next action genuinely can't run until the overlay is
back.

Verified two ways: `test_cursor_reassert_fast_path.py` drives the real
`cursor_driver.mjs` process directly and deliberately does NOT reset
`window.name` after a second navigation (simulating the exact race the old
code was vulnerable to) -- the fast path still finds the tracked tab and
remounts the overlay in single-digit milliseconds, something the old
poll-by-`window.name` implementation could not have done at all in that
scenario. `test_cursor_navigate_await_fix.py` goes one level up, through
the real `guarded()` wrapper against a real `BrowserToolProvider` and a
real local Chromium: it calls the actual guarded `browser_navigate` tool
twice in a row and checks -- with no sleep, immediately after each call
returns -- that the overlay is already mounted, which is only possible if
the re-assertion completed before the awaited call handed control back.
Both existing cursor tests (`test_cursor_overlay_real_fix_e2e.py`,
`test_cursor_driver_wiring_live.py`) still pass unchanged.

## Making deepsearch faster -- where the 16 minutes likely went, and the options

You reported a JAX -> SF flights + hotel planning task taking 16 minutes,
and asked me to think through where that time is actually going and lay
out options rather than just pick one. I don't have a trace from your
actual run (Browserbase/OpenRouter logs aren't reachable from here), so
this is grounded in reading the real code paths that run on every step,
not a profiler output -- I've said below, for each one, how confident I am
and why.

**High confidence -- this is a real, uncapped cost on every single click,
type, hover, and dropdown selection:** `CURSOR_ANIMATED_TOOLS` (`browser_click`,
`browser_type`, `browser_hover`, `browser_select_option`) all synchronously
`await self._move_cursor_to(...)` before the real action runs. That calls
into `human-cursor`'s `moveTo()`, which generates a bezier path of
**35-80 individual points** (its own randomized default) and dispatches
each one as a SEPARATE, individually-awaited `page.mouse.move()` CDP call
-- no batching, no pipelining. Over a remote Browserbase connection, each
of those round trips is real network latency, not a local call. A single
click's "move the mouse there first" step alone can plausibly cost
1-3+ seconds before the click even happens, and a flights+hotel booking
flow (search fields, date pickers, passenger/cabin dropdowns, result
filters, picking a specific flight, repeating for a hotel) easily has
20-50+ such interactions. This is very likely the single largest
controllable cost in the run.

**Medium-high confidence -- likely serialized when it didn't need to be:**
`delegate_website_task`'s own docstring and system-prompt guidance
(`DEEPSEARCH_SYSTEM_PROMPT`) frame concurrent delegation around "compare
this product's price across site A, B, C" -- it never says "flights on one
site and a hotel on another are exactly this kind of independent work
too." A flight search and a hotel search are genuinely independent (neither
needs the other's result to proceed), which is exactly the shape
`delegate_website_task` was built to parallelize -- confirmed elsewhere in
this README that concurrent sub-workers on a shared Browserbase session
really do run at the same time, not one after another. Nothing in the
prompt currently nudges the model to recognize a trip-planning request as
that shape, so it likely did the whole thing in one tab, one leg after the
other.

**Medium confidence, already partially mitigated:** `DEEPSEARCH_SYSTEM_PROMPT`
already tells the model to prefer `browser_find`/a depth-limited
`browser_snapshot` over a full snapshot when possible ("Speed matters --
users notice how long this takes"). Whether the model actually followed
that reliably on your run, I can't tell from here -- a full accessibility
tree on a flights-results page (calendar widgets, filters, dozens of result
rows) is large, and every extra KB in a snapshot is both slower and more
expensive on every subsequent model call that still has it in context.

**Lower confidence / smaller likely contribution:** the fixed per-run
startup cost (opening a Browserbase session, spawning `@playwright/mcp` and
`cursor_driver.mjs`) is real but probably only single-digit seconds against
a 16-minute total. `SUBAGENT_MODEL_NAME` (what deepsearch already runs on)
is already the "flash"/fast tier, not the heavier orchestrator model, so
switching models is unlikely to be a large additional win by itself. The
reading-animation/idle-breathing cosmetic scroll does NOT add latency by
design -- it only fills think-time gaps that were already happening, so
turning it off would change nothing about total run time (flagging this so
effort doesn't go there by mistake).

**Options, roughly in order of expected impact for the effort involved:**

1. **Tune down human-cursor's realism** -- pass a lower point-count/spread
   into `createCursor`'s options in `cursor_driver.mjs` (or add a
   `DEEPSEARCH_CURSOR_SPEED` config knob with fast/medium/human presets).
   Cuts the biggest identified cost directly; the visible trade-off is a
   slightly less organic-looking mouse path. Low risk, moderate effort.
2. **[IMPLEMENTED -- see "Deepsearch speed fixes" below] Teach
   delegate_website_task to recognize trip-planning as parallelizable** --
   add "book a flight and find a hotel" (and similar: flight + car rental,
   comparing two unrelated services) as an explicit example of independent
   multi-site work in `DEEPSEARCH_SYSTEM_PROMPT`. No infrastructure change,
   no new failure mode -- purely a prompt clarification riding on machinery
   that's already built and tested. Low risk, low effort, likely large win
   specifically for multi-leg trip requests like this one.
3. **[IMPLEMENTED -- see "Deepsearch speed fixes" below] Batch/pipeline the
   cursor's mouse-move dispatch** instead of awaiting each of the 35-80
   points individually -- send them without waiting for each ack, only
   confirming the last one, so the real network cost drops from "N round
   trips" to close to "1 round trip" while still visually tracing the same
   path. Bigger win than #1 with no visual trade-off at all, but it means
   patching/wrapping `human-cursor`'s own move logic rather than just
   tuning its options -- more engineering effort and a little more risk of
   a subtle regression in the drawn path's smoothness.
4. **Skip the full human-cursor animation for "minor" interactions**
   (typing into a field, selecting a dropdown option) and reserve it for
   clicks a user would actually watch happen -- fewer slow moves overall.
   Requires a judgment call about which actions "deserve" realism; medium
   effort, and changes the character of the live view (some actions would
   visibly snap rather than glide).
5. **Reinforce/verify the snapshot-size guidance** -- e.g. a deterministic
   nudge (same shape as this codebase's other backstops) if a snapshot
   comes back over some size threshold, pushing the model toward
   `browser_find`/`depth=` on the next call instead of relying on it
   following the system prompt's advice unprompted. Moderate effort,
   uncertain size of win without real trace data to confirm how often this
   actually happens.

I haven't implemented any of these -- wanted you to see the reasoning and
pick, especially since #1/#3/#4 all trade some amount of "looks human" for
speed, which is a product call, not just an engineering one.

**You picked #2 ("parallelize trip legs") and #3 ("batch the cursor's
mouse-move dispatch") -- both implemented, neither trades away any visual
realism.** #1, #4, and #5 above remain unimplemented and available if the
run is still too slow after these land.

## Deepsearch speed fixes: parallel trip legs + pipelined cursor movement

**Parallelize trip legs.** `_delegate_website_task`'s own docstring (which
IS the tool description the model sees -- it's registered via
`StructuredTool.from_function(..., description=(self._delegate_website_task
.__doc__ or "").strip())`) and `DEEPSEARCH_SYSTEM_PROMPT`'s "SEVERAL
INDEPENDENT websites" guidance both previously only framed concurrent
delegation around comparing the same thing across multiple sites ("this
product's price across site A, B, C"). Neither ever said a trip's
independent LEGS -- flights on one site, a hotel on another, a rental car on
a third -- are exactly the same shape of independent work. Both now say so
explicitly, with the flights/hotel/rental-car example spelled out and an
explicit "don't work through legs serially just because the request reads
as one task" instruction, plus the same caveat as the existing guidance:
only delegate legs in parallel when they're genuinely independent (a hotel
search that depends on which flight dates actually got booked still has to
wait for that result first). No infrastructure changed -- LangGraph's
`ToolNode` already runs multiple tool calls issued in the same model turn
concurrently (confirmed elsewhere in this README), so this is purely
teaching the model, via its own tool description and system prompt, to
recognize a trip-planning request as "call delegate_website_task once per
leg, all in the same turn" instead of working through it serially in one
tab. There's no automated test for prompt wording itself (it's an
instruction to the model, not deterministic logic) -- I verified the edited
text reads correctly and re-ran the full regression suite to confirm
nothing else broke.

**Batch/pipeline the cursor's mouse-move dispatch.** The actual mechanism
behind the "35-80 individually-awaited round trips per move" cost described
above: human-cursor's own `tracePath()` (in
`node_modules/human-cursor/lib/spoof.js`, not something this codebase
wrote or can pass options into) walks its generated bezier path one point
at a time, `await page.mouse.move(v.x, v.y)`-ing each before starting the
next. `cursor_driver.mjs`'s `move` command handler now wraps the target
page's `page.mouse.move` in a `pipelineMouseMoves()` helper before calling
`cursor.moveTo()`: the wrapper fires the real CDP call immediately but
resolves its own promise right away, so tracePath's loop races on to
queueing the next point instead of blocking on this one's network round
trip. All N calls hit the wire back-to-back in order (a single CDP
connection is one WebSocket, so receipt order is preserved even though we
don't wait for each ack) -- the bezier path itself is still generated by
human-cursor's own untouched curve logic, exactly the same points in
exactly the same order, only the dispatch is pipelined instead of
serialized. The `move` command still doesn't return until every real move
has actually landed (`pipeline.settle()` awaits all of them), and a failed
individual move is swallowed with a log line the same way tracePath's own
built-in tolerance already worked, so callers can't tell the difference
except that it's faster. Verified with a new test,
`test_cursor_pipeline_speed.py`, against the real `cursor_driver.mjs`
process: since local Chromium round trips are sub-millisecond (too fast to
show a serial-vs-pipelined difference on their own), the driver has a
test-only `MESSA_CURSOR_DRIVER_TEST_DELAY_MS` env var (unset/zero-cost in
production) that stands in a fixed artificial per-call delay for what a
real remote Browserbase round trip would cost. With a 40ms artificial delay
and a real 47-point move, the old serial behavior would have cost roughly
47 x 40ms = 1880ms; the pipelined version completed in 97ms -- about 19x
faster on that run, while the test also confirms it still took at least one
real delay period (proving it didn't just skip the network cost, only
stopped serializing it). Also re-ran `test_cursor_reassert_fast_path.py`,
`test_cursor_overlay_real_fix_e2e.py`, and `test_cursor_navigate_await_fix.py`
against the changed file -- all still pass, so the pipelining doesn't
interfere with the fast-path re-registration or the awaited re-assertion
fix from the cursor-invisibility bug above.

**Not implemented / still on the table:** tuning down human-cursor's point
count or spread (#1) and skipping the full animation for "minor"
interactions like typing/dropdowns (#4) -- both still available if the run
is still slower than you'd like. #5 (a deterministic backstop nudge for
oversized snapshots) WAS implemented since this note was written -- see
"Token-cost optimization" below, where it landed as part of the token-cost
work rather than the speed work, since a large snapshot is exactly as
expensive in tokens as it is slow in latency.

## Token-cost optimization: what each agent's prompts actually cost, and where compaction was silently broken

You asked whether the prompts sent to each agent could be made more
token-efficient without losing quality -- and whether quality could improve
too. Short answer: yes to both, and the biggest win wasn't prompt wording
at all. Four things landed, in order of impact:

**1. deepsearch was running with ZERO auto-compaction -- not a tuning
problem, a missing-safety-net problem.** This is the one I'd flag as the
real finding here. Every agent in this project sits on top of LangChain/
deepagents machinery that's supposed to compact a conversation's history
once it gets too large (summarize the old stuff, keep the recent stuff, so
a long-running agent doesn't keep re-paying for its entire past on every
single step). I checked whether that was actually happening, and it wasn't,
for two separate reasons:

- The four subagents built via deepagents' `create_deep_agent` (Messa
  herself, executive_assistant, email_agent, document_agent,
  routines_agent) DO get auto-compaction, but its trigger threshold comes
  from `model.profile["max_input_tokens"]` -- and I confirmed directly
  (`config.build_model(...).profile` prints `None`) that a ChatOpenAI
  instance built from an OpenRouter model string like
  `"~deepseek/deepseek-v4-pro"` has no profile at all, because LangChain's
  profile lookup only recognizes OpenAI's own published model names.
  With no profile, deepagents falls back to a generic default: don't
  compact until the conversation hits 170,000 raw tokens, then keep only
  the last 6 messages. Every agent in this project has been running on
  that fallback the whole time, regardless of anything in its system
  prompt.
- deepsearch itself -- the agent that actually needed this most, since it
  runs up to `DEEPSEARCH_MAX_STEPS=100` steps and appends a fresh
  `browser_snapshot` on most of them -- doesn't go through
  `create_deep_agent` at all. It calls `langchain.agents.create_agent`
  directly (both for the top-level agent and each `delegate_website_task`
  sub-worker), and that function's own `middleware` parameter defaults to
  an empty tuple -- confirmed by reading its signature, not assumed. So
  deepsearch wasn't even getting the 170k fallback the other agents got by
  default. It had no compaction of any kind.

Why this matters more than prompt wording: a chat-completions-style agent
loop resends its ENTIRE message history on every single step -- there's no
server-side conversation state to lean on. So total tokens billed across an
N-step run is dominated by that growing history, not by the comparatively
fixed cost of the system prompt/tool schemas -- it's roughly quadratic in
N, not linear. A 100-step deepsearch run appending multi-thousand-token
snapshots most of the way through was quietly compounding cost for the
entire run's length, every time.

Fix, two parts: `config.build_model()` now accepts
`effective_context_tokens=` and stamps it onto the returned model's
`.profile` (confirmed to be a plain writable attribute, not something
LangChain protects) -- `registry.py`'s two call sites pass
`ORCHESTRATOR_EFFECTIVE_CONTEXT_TOKENS` (120,000, a lighter touch, since
Messa's own conversation with you is closer to an ongoing relationship than
a bounded task) and `SUBAGENT_EFFECTIVE_CONTEXT_TOKENS` (60,000, more
aggressive, since a subagent's job is bounded and old tool-call noise from
early in one task is rarely worth keeping). deepsearch itself needed its
own fix on top: `_deepsearch_summarization_middleware()` in
`deepsearch_tools.py` builds a real `langchain.agents.middleware
.SummarizationMiddleware` (trigger at 85% of the subagent budget, keep the
last 10%, same split deepagents' own default logic uses) and both of
deepsearch's `create_agent(...)` calls now pass it explicitly via
`middleware=[...]`. No filesystem-backed history offload (deepagents' own
version supports that, for a model that can `read_file` an evicted-history
file back later) -- deepsearch has no filesystem tools at all, so that
would just be unreachable dead weight; evicted detail becomes the LLM's own
summary and nothing more, which is fine here since deepsearch's job is to
finish the task and report back, not serve as a queryable transcript of
exactly what it clicked 40 steps ago.

Verified with a new test, `test_token_compaction.py`, against the real
`deepagents.middleware.summarization.compute_summarization_defaults` and
the real `langchain.agents.middleware.SummarizationMiddleware` -- nothing
about either is mocked. It confirms the profile-stamping actually flips
deepagents' own trigger from the 170k fallback to the fraction-based path,
and then proves REAL compaction on a fabricated 121-message, 61,922-token
history (20 fake browser_snapshot-sized tool results): after
`before_model()` runs, the history drops to 11 messages and 5,188 tokens --
a 92% reduction, achieved by calling the actual production code, not by
asserting a config value looks right.

**2. Trimmed the system prompts and tool docstrings that get resent every
single step.** The literal thing you asked about. Measured with
`count_tokens_approximately` (the same approximate counter deepagents
itself uses for its real trigger decisions, not an arbitrary estimate) --
before -> after:

- `DEEPSEARCH_SYSTEM_PROMPT`: 1,013 -> 706 tokens (-30%)
- `delegate_website_task`'s tool description: 620 -> 454 tokens (-27%)
- `request_human_help`'s tool description: 275 -> 236 tokens (-14%)
- `_SUBAGENT_SYSTEM_PROMPT` (each delegate_website_task sub-worker):
  452 -> 382 tokens (-15%)
- Messa's own orchestrator system prompt: 1,397 -> 1,278 tokens (-8.5%)

Two kinds of cuts, both quality-neutral or quality-positive rather than
quality-risking: (a) genuine redundancy, like `delegate_website_task`'s
docstring and `DEEPSEARCH_SYSTEM_PROMPT`'s own bullet about it separately
re-explaining the same rules in slightly different words -- now the system
prompt just points at the tool's own description instead of duplicating it,
which also removes a real maintenance hazard (two copies of the same rule
can drift out of sync, which had already started happening); (b) internal
dev-facing asides that were sitting INSIDE model-facing docstrings by
accident -- `/tmp/test_mcp_multitab.py`-style file citations and
`db.create_human_help_request`-style code pointers that a human maintainer
benefits from but the model gets zero behavioral value from, since it's
resent as part of the tool schema on every call regardless. Those moved to
plain `#` comments next to the function instead of disappearing.

Deliberately left alone: Messa's "Critical: that acknowledgment and the
task tool call must be in the SAME response" paragraph and her
"Deepsearch sessions" paragraph, both of which exist specifically to
prevent previously-diagnosed real bugs (the "empty promise" delegation-
dropping failure, and session-continuity guessing). Their repetition reads
as verbose, but I'm not confident cutting it wouldn't reintroduce exactly
the bug it was written to prevent, and "sacrificing quality" is the one
thing you explicitly asked me not to do -- so those two stayed word-for-
word. Email/document/routines subagents' prompts were already lean
(109-168 tokens) and weren't worth the edit risk for a handful of tokens
each.

**3. Enforced the snapshot-size guidance that used to be advisory-only.**
`DEEPSEARCH_SYSTEM_PROMPT` already told the model to prefer
`browser_find`/`browser_snapshot(depth=...)` over a full snapshot, but
nothing enforced it -- and an oversized snapshot a model takes anyway
doesn't just cost once, it gets resent in full on every subsequent step
until compaction (above) eventually evicts it. `guarded()`'s
`browser_snapshot` success path now calls the new
`_nudge_if_oversized_snapshot()`: a non-depth-limited snapshot that comes
back over `config.DEEPSEARCH_LARGE_SNAPSHOT_CHARS` (12,000, a round,
generous default with no real production traffic to calibrate against yet)
gets a short corrective line appended to its own returned text, same "one
corrective nudge into the same graph thread" shape as the existing
auth-wall backstop a few lines below it. This was flagged, unimplemented,
after the speed investigation above (option #5) -- it landed here instead
because the same oversized snapshot is exactly as expensive in tokens as
it is slow in latency, so it belongs with this work.

Verified with a new test, `test_snapshot_size_nudge.py`, against a real
local Chromium page built with 220 list items specifically so its real
accessibility-tree snapshot exceeds the threshold (46,634 characters,
confirmed, not assumed) -- and confirms three things: the oversized full
snapshot gets the nudge, the exact same page's `depth=1` snapshot does NOT
(already the disciplined choice, nothing to correct), and an ordinary small
page's full snapshot does NOT either (must not fire on everyday use).

**4. Investigated real provider-side prompt caching -- didn't implement
anything, here's why.** This is the theoretically biggest lever (a cache
hit is typically a fraction of the cost of a fresh read), so it was worth
checking carefully before writing it off. Two findings:

- deepagents' `create_deep_agent` DOES automatically attach prompt-caching
  middleware to every agent it builds -- but reading
  `deepagents/middleware/_prompt_caching.py` directly shows it only ever
  constructs Anthropic, Bedrock, and Fireworks caching middleware, each
  with `unsupported_model_behavior="ignore"`. Since this project's models
  resolve to provider `"openai"` (ChatOpenAI pointed at OpenRouter -- see
  `registry.py`'s own comment on this), all three silently no-op. This
  machinery has never done anything for any agent in this project.
- Separately, `langchain_openai`'s `ChatOpenAI` itself DOES support OpenAI's
  own native prompt-caching controls (`prompt_cache_key`,
  `prompt_cache_options`, `prompt_cache_retention`) and parses a
  `cached_tokens` field back out of the response's usage data -- a real,
  currently-unused capability. Whether OpenRouter's proxy to your specific
  DeepSeek-family model actually honors any of this, or does its own
  automatic caching independently of anything in this codebase (DeepSeek's
  own real API is documented to cache automatically, no code changes
  needed, when a request's prefix matches a recent one), isn't something I
  can verify from this sandbox -- I don't have a live OpenRouter account
  to test against, deliberately, same as every other such caveat already
  in this README.

I didn't wire in `prompt_cache_key` blindly, on purpose: picking a cache
key that's too broad (e.g. one static key shared by every user) risks
concentrating traffic onto one backend node if OpenRouter actually honors
it, which could hurt rather than help at real scale, and I have no
production telemetry from this sandbox to tune it correctly either way.
**What I'd recommend instead:** after this ships, check a real response's
`usage` object (or your OpenRouter dashboard) for a `cached_tokens` or
similar field. If it's already nonzero, some caching may already be
happening automatically on OpenRouter/DeepSeek's side, independent of
anything here -- in which case the compaction and prompt-trimming fixes
above are still exactly the right ones (they shrink what there is to cache
or resend either way), and a `prompt_cache_key` change becomes a smaller,
better-informed follow-up rather than a guess.

**Net effect, honestly graded:** the compaction fix (#1) is the one I'd
point to first -- it's concentrated on exactly the runs that were costing
the most (a short deepsearch task that never approached 170k tokens saw no
difference either way; a long one now gets compacted in bounded increments
instead of ballooning unchecked for up to 100 steps, demonstrated at a 92%
reduction on the fabricated test history above). The prompt/docstring trims
(#2) are smaller per-run but apply to every single call across every agent
and every user, so they compound across volume -- roughly 20-30% off
deepsearch's per-step fixed cost specifically, the agent that runs the most
steps. #3 compounds with #1 by keeping the largest recurring item smaller
in the first place. #4 remains a real, unverified opportunity -- worth
revisiting once you can see real cache-hit data.

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

## CTO review of six speed/UX questions -- what got implemented

You asked me to weigh in, CTO-style, on six things after watching a live
deepsearch run: parallel sub-agent delegation not actually saving time,
the human cursor never appearing at all, pre-searching before delegating,
page-transition animations, live-view tile names, and slow page reads.
You approved implementing everything discussed. Here's what actually
shipped, plus two things I found along the way that weren't on your list.

**The real reason the cursor never appeared (found while investigating
#2, more fundamental than the question you asked).** You'd noticed the
human-cursor driver's real mouse movement never showed up in the live
view and wondered whether `--init-script` silently doesn't fire when
connecting over `--cdp-endpoint`. I tested that directly with a real
Playwright `pageerror` listener and it does fire -- the actual bug was one
level deeper: `cursor_overlay.js`'s very first block (the one that hides
the live-view scrollbar) called `.appendChild()` on
`document.head || document.documentElement` unconditionally, before
checking either existed. At the exact moment an init script runs on a
fresh navigation, `document.documentElement` can still be `null`, which
threw synchronously and aborted the ENTIRE script -- not just the
scrollbar tweak, but the cursor arrow, the reading animation, and (once
built) the new click/typing effects too. This explains why the cursor
was invisible on every single run, not intermittently. Fixed by deferring
that `appendChild` behind an existence check with a `DOMContentLoaded`
fallback, the same pattern the file already used elsewhere. Verified with
a direct `pageerror` probe (no more crash) and against the full
`--cdp-endpoint`/`--shared-browser-context` production shape, including a
tab opened after the initial connection (covers `delegate_website_task`'s
sub-worker tabs too).

**#2: dropped the real human-cursor driver; kept and improved the cheap
DOM-only one.** The separate Node process (`human-cursor` connecting over
its own CDP connection, correlating tabs by a `window.name` marker) added
real complexity and a second point of failure for a visual that, once the
crash above is fixed, the existing lightweight DOM-drawn arrow already
delivers. Removed `CursorDriver` entirely (~130 lines), the marker
correlation logic, `messa/nodehelpers/cursor_driver.mjs`, and the
`messa/package.json`/`package-lock.json` + Dockerfile `npm install` step
that only existed to support it -- one less native dependency in the
image. `DEEPSEARCH_HUMAN_CURSOR_DRIVER` is gone from `config.py`;
`DEEPSEARCH_CURSOR_OVERLAY` (still default `true`) now controls the DOM
arrow alone.

While in there, fixed an unrelated latency bug the crash had been
masking: `_move_cursor_to`'s `browser_evaluate` call used to be awaited
inline before every real click/type/hover/select_option, so each action
paid the cursor's own ~380ms CSS-transition round trip as pure added
latency, N times per run. It's now fired via `asyncio.create_task`
(`_fire_cursor_move`, same fire-and-forget pattern `_start_reading_animation`
already used) and tracked in `self._cursor_move_task` for cleanup in
`__aexit__` -- the visual still happens, it just no longer blocks the
action behind it.

**Click ripple + typing highlight (your favorite of the ideas).** Riding
the same evaluate call the cursor move already fires (zero added
round trips), `cursor_overlay.js` now exposes
`window.__messaEffects = {ripple, highlightTyping, clearTypingHighlight}`:
`browser_click` triggers an expanding, fading ring at the click point;
`browser_type` outlines the target field for ~1.2s then self-clears.
Both gated by new `DEEPSEARCH_CLICK_RIPPLE`/`DEEPSEARCH_TYPING_HIGHLIGHT`
flags (default `true`).

**#4: page-transition flash, done without any Python-side signaling.**
Since a real click/type/etc. already re-injects the init script on every
navigation, the flash (`DEEPSEARCH_PAGE_TRANSITION_FLASH`, default
`true`) is self-triggering: a brief green overlay that fades via
`requestAnimationFrame`, firing on `DOMContentLoaded` or immediately
depending on `document.readyState`. No new tool call, no new latency.

**#5: fixed the live-view tile heading collision (the real bug behind
your "tiles should show the actual tab name" ask).** Digging into
`server.py`'s live-tile tracking, `_host_key(url)` matched by hostname
alone, so Google Flights (`.../travel/flights`) and Google Hotels
(`.../travel/hotels`) collided onto the same tracked tile -- explaining
why tile names looked wrong/generic rather than just "not the real title."
Fixed by including the URL path in `_host_key` (still tolerant of
www/trailing-slash/query-string drift, verified against the existing
`part6_host_fallback_matching_survives_url_drift` test), and by preferring
Browserbase's own reported page `<title>` over a hostname-derived guess
whenever a real, non-blank title is available (`_real_page_title`). New
test: `test_live_tile_title_fix.py`.

**#1: instrumented before guessing.** Rather than jumping straight to a
model pool on your suggestion alone, added a `_TimingCallback`
(`langchain_core.callbacks.AsyncCallbackHandler`) that logs real
per-LLM-call latency, tagged by tab id, on both the top-level run and
every `delegate_website_task` sub-worker, plus queue-wait and total
sub-worker wall-clock time. Run a real multi-site task next and the
console log will show whether the ~20-minute run is dominated by LLM
call latency (points at a shared-key concurrency ceiling) or by
everything else (browser/CPU contention, serial waits) -- that answer
should drive whether the key pool below is actually the fix or a
different lever is needed. Implemented the pool anyway since you'd
already decided you wanted it: `SUBAGENT_API_KEY_POOL`
(`MESSA_OPENROUTER_API_KEY_POOL`, comma-separated) round-robins
concurrent sub-workers across separate OpenRouter keys via
`_pick_subagent_model`; with the default single-key setup (nothing
configured) it's a complete no-op, returning the exact same model
instance, so nothing changes for you until you actually set the env var.

**#3: pre-search guidance added to the orchestrator's existing
toolset -- no new tool.** `DEEPSEARCH_SYSTEM_PROMPT` now tells the
top-level agent that if it doesn't already know the right URL (a specific
results page, not just a homepage), it should do one quick search itself
first (google.com or perplexity.ai, in its own tab) for a deep-link or
reference fact, then delegate with that as a head start -- framed as
optional ("skip it once you already have a working URL"), and explicitly
warned never to treat a search result as today's live price/availability
data (`delegate_website_task` stays the source of truth for that). Costs
about 117 tokens per run (706 -> 823 for this prompt) for the cases where
it actually helps.

**#6: a two-tier deterministic backstop on oversized snapshots, not an
"interactive-only" filter.** I considered filtering `browser_snapshot`'s
default output to interactive elements only, to make it faster for the
model to parse, and rejected it -- deepsearch's real work (reading
flight/hotel prices) depends on static text content, and an
interactive-only filter would strip exactly that. Instead,
`_apply_snapshot_size_backstop` (renamed from
`_nudge_if_oversized_snapshot`) now has two tiers: past
`DEEPSEARCH_LARGE_SNAPSHOT_CHARS` (12,000, unchanged) it still just
appends the existing corrective nudge; past a new
`DEEPSEARCH_SNAPSHOT_HARD_CAP_CHARS` (60,000, deliberately far above the
first tier so ordinary large pages are never touched) it actually
truncates the returned text and appends a notice with real kept/total
character counts and a suggestion to use `browser_find`/`depth=` instead.
This bounds worst-case latency and token cost on genuinely extreme
outlier pages without changing behavior for anything smaller.

**Verification.** All of the above went through the full existing
regression suite (`test_live_tiles.py`, `test_multisite_delegation_live.py`,
`test_token_compaction.py`, `test_dashboard_route.py`,
`test_default_briefings.py`, `test_executive_quality_check.py`, and
others) plus new tests written for each change
(`test_live_tile_title_fix.py`, `test_cursor_overlay.py` rewritten for
the new fire-and-forget signature, `test_visual_feedback_live.py` for a
live end-to-end check of the ripple/highlight effects and dispatch
latency, `test_subagent_pool_and_timing.py`, `test_presearch_guidance.py`,
`test_snapshot_size_nudge.py` extended with a genuinely-huge-page case for
the new truncation tier) -- all passing.

## Real Gmail OAuth connections, per user, via Composio

The email agent used to be a single shared inbox (`COMPOSIO_EMAIL_CONNECTED_ACCOUNT_ID`,
one Composio-connected account for the whole app) and its Composio SDK
calls were written against docs but never run against a live account. You
asked for the real thing: each user connects their own Gmail via a proper
OAuth flow, with read + write access (not a blanket full-account grant),
and can trigger it either during onboarding (opt-in) or any time later by
just asking. Only `COMPOSIO_API_KEY` needs adding to your env -- everything
else below is automatic.

**Verified against the actual SDK, not just its docs.** Composio's own
documentation describes more than one still-partially-current way to start
an OAuth connection, and one of them (`connected_accounts.initiate()`) is
mid-retirement specifically for this exact case (Composio-managed auth on a
redirectable scheme like Gmail's OAuth2) on a rolling cutover between
2026-05-08 and 2026-07-03 -- today is past that window. Rather than build
against whichever example page turned up first, I downloaded and read the
actual installed package sources (`composio==0.21.0`, its `composio-client`
dependency) directly from PyPI to confirm the real, current method
signatures, return shapes, and exception types. This is why the connect
flow below uses `connected_accounts.link()`, not `.initiate()`.

**How connecting works.** A new tool, `request_email_connection`
(email_agent), asks Composio for a Gmail connect link scoped to exactly the
four Gmail actions this project calls -- fetch/search, get one message,
send, reply -- via `auth_configs.create`'s `tool_access_config`, which has
Composio compute the *minimum* OAuth scopes needed for those specifically.
That's the actual mechanism behind "read, write privileges to gmail," not a
blanket account grant. The auth config itself is found-or-created
automatically and reused by name across restarts, so a redeploy doesn't
spin up a fresh one (and a fresh consent screen) every time;
`COMPOSIO_GMAIL_AUTH_CONFIG_ID` overrides this if you ever want to point at
your own custom Gmail OAuth app instead.

The link itself is texted to the user directly from inside the tool, not
typed out by the model -- same reasoning as the deepsearch live-view link
(see cli.py's run_message docstring): a long Composio URL is exactly the
kind of string an LLM can subtly mangle mid-reply, so the tool sends it via
Sendblue itself and returns email_agent a short confirmation to relay
instead ("sent, don't repeat the link"). In the CLI, there's no phone to
text, so the link is returned directly in the tool's own output, where the
CLI's console tracing already shows it. An already-connected user calling
this again gets a friendly "already connected" message (Composio raises
`ComposioMultipleConnectedAccountsError`, caught explicitly) rather than a
raw exception or a second, redundant link.

**Confirming it actually connected.** `request_email_connection` doesn't
block waiting for the user to finish an OAuth flow that might take seconds
or hours -- it persists a row to a new `email_connection_requests` table
(`migrations/012_email_connection.sql`) and returns immediately. A new
background loop, `server.py`'s `_production_email_connection_poll_loop`
(same decoupled shape as the existing deepsearch-pause and cron/reminder
loops), polls Composio every 20s for that row's real status: ACTIVE flips
`users.email_connected` (a cheap cached flag read every turn, so Messa/
email_agent know without an extra Composio call) and sends one confirmation
text ("Your Gmail is connected!"); a terminal failure (FAILED/EXPIRED/
REVOKED) expires the row and tells the user it didn't go through; anything
still in progress is left alone until the next poll. Requests older than
`EMAIL_CONNECTION_REQUEST_EXPIRES_HOURS` (24h default) expire silently --
the user can just ask again. A `check_email_connection_status` tool also
lets the user ask "is it connected yet?" on demand rather than waiting.

**Onboarding.** A fourth onboarding question, `awaiting_email_connect`,
now runs after location (before "complete"): Messa asks if the user wants
her to handle their email, framed as optional and reversible. Saying yes
calls `save_profile_info('connect_email', 'yes')` (which only advances
onboarding -- there's no `connect_email` column, deliberately, since the
real `email_connected` flag is only ever set once Composio actually
confirms the connection) AND delegates to email_agent's
`request_email_connection` in the same response, so the link goes out
immediately rather than needing a second round trip. Saying no just
advances onboarding with nothing else happening -- exactly your "if not,
they can always send connect my gmail/email" fallback, since
`request_email_connection` is available any time after onboarding too.

**A real, pre-existing bug fixed along the way.** Every Composio SDK call
(the old single-account code included) is synchronous -- a plain
`requests`-based client -- but the old `_execute` called it directly inside
`async def` tool functions, meaning any Gmail API call would block the
*entire* asyncio event loop for its duration: every other user's turn, and
the FastAPI webhook handler itself, frozen until that one call returned.
Every Composio call in the rewritten `email_tools.py` (including the
existing read/send actions) now goes through `asyncio.to_thread`.

**Verification.** New tests: `test_email_oauth_tools.py` (auth-config
find-or-create/memoization/scoping, the SMS-vs-CLI link delivery split with
an explicit check that the raw URL never appears in the model-facing return
text for a real channel, already-connected handling, and the
`email_connected=False` short-circuit firing *before* any Composio call),
`test_email_connection_poll_loop.py` (ACTIVE/terminal-failure/still-pending
status transitions, run against the real loop body the same way the
existing `test_pause_loop.py` tests its sibling), and
`test_onboarding_and_email_db.py` (the new onboarding step's DB-level
advance/no-column-write behavior, plus the full email_connection_requests
lifecycle and its pre-migration no-op safety). None of these needed a live
Composio account -- the SDK's own exception types and response shapes are
faked directly from the real package sources read above, not guessed at.
Full existing regression suite (16 files total) re-run and passing
alongside these three new ones.

**Update after your own first real run against a live Composio account:**
two things this project's own SDK-source-reading couldn't have caught,
because they're facts about the live API surface, not the client library --
`GMAIL_GET_MESSAGE` isn't a real action in the Gmail toolkit despite
looking like an obvious one, and `tools.execute` requires either a
`version` or `dangerously_skip_version_check=True` or it raises
`ToolVersionRequiredError`. See the "Known Phase 1 limitations" bullet
below for the fixes; `_SLUG_GET`, `_GMAIL_AUTH_CONFIG_NAME`, and
`_execute_sync` in `email_tools.py` all changed as a result, plus a new
`COMPOSIO_TOOLKIT_VERSION` env var. Also fixed in the same pass (found
because a broken import here blocks the whole app at startup, which is
what made it impossible to even get to testing the email agent):
`web_search_tools.py`'s `ddgs` import now falls back to the older
`duckduckgo-search` package name if `ddgs` itself isn't actually
importable in the deployment environment.

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

**Updated scope (see "Executive assistant rebuild" above for the full
story):** only SCHEDULING goes through `pending_actions` now --
`create_calendar_event`, `update_calendar_event`, `delete_calendar_event`,
and `create_recurring_cron`. Your schema's `action_type` enum originally
also covered `create_task`/`update_task`/`delete_task`/`create_reminder`/
`cancel_reminder`/`delete_note`, and an earlier round of this project gated
all of those too -- that's been narrowed per your explicit correction
("approval only for scheduling... reminders and tasks do not need
approval"). `db.GATED_ACTION_TYPES` is the authoritative set; it's
enforced in Python (`propose_action` raises on anything not in it), not by
an actual Postgres enum constraint.

1. `executive_assistant` (calendar events) / `routines_agent` (new
   recurring jobs) call a `propose_*` tool -> inserts into
   `pending_actions`, nothing changes yet.
2. Messa relays the proposal to you in the chat and waits for an explicit
   yes.
3. You confirm -> Messa (only Messa -- subagents can't do this
   themselves) calls `confirm_pending_action`, which applies the change
   and writes an `audit_logs` row. A no calls `reject_pending_action`.

Everything not in that set -- reads, tasks, reminders, notes, contacts --
writes directly, no round trip.

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

Finally, there was no way to tell "this user's timezone was actually
resolved from something real" apart from "this is still the untouched
default" (see "Executive assistant rebuild" above), so
`migrations/007_timezone_confirmed.sql` adds `users.timezone_confirmed`
(defaults `FALSE`).

All seven are additive/reversible and the app runs fine without them
(project tracking, email-saving, and session resumption just no-op;
without migration 005, deepsearch still runs against Browserbase, it just
can't remember your login between separate sessions or capture a live-view
link; without migration 006, deepsearch works exactly the same, Messa just
never has a link to mention; without migration 007, timezone resolution
still runs and `users.timezone` still gets updated, it just can't persist
the "was this actually confirmed" flag, so the system prompt's "ask for a
zip code" nudge won't fire).

## Known Phase 1 limitations (by design -- later phases cover these)

- Single hardcoded CLI user (`MESSA_CLI_USER_PHONE` in `.env`, defaults to
  a placeholder) -- goes through onboarding once, like any new user, then
  stays "known" for future CLI runs. Phase 2 resolves the user from the
  inbound channel instead of one fixed phone number.
- ~~Routines agent's cron jobs are created/scheduled but not actually
  executed on a timer yet~~ -- **fixed**: `server.py`'s
  `_production_cron_loop` (started on FastAPI startup) actually
  re-invokes Messa on schedule and delivers the reply over Sendblue now.
  `messa/background.py`'s poller still only prints what *would* fire, but
  that's intentional -- it's the CLI's console-only preview, not the
  production path (see "Executive assistant rebuild" above).
- Email agent's Composio integration (see "Real Gmail OAuth connections,
  per user, via Composio" above) was written against the actual installed
  SDK's source -- method signatures, return shapes, and exception types
  confirmed by reading `composio`/`composio-client` directly, not just
  docs -- and has now also been run against a real Composio account, which
  is how two real bugs surfaced that no amount of source-reading
  would've caught: `GMAIL_GET_MESSAGE` isn't an actual action in the live
  Gmail toolkit (fixed by reusing `GMAIL_FETCH_EMAILS` filtered to one
  message id instead -- see `email_tools.py`'s `_SLUG_GET`), and
  `tools.execute` unconditionally raises `ToolVersionRequiredError` unless
  a `version` or `dangerously_skip_version_check=True` is passed (fixed in
  `_execute_sync`; pin a version via `COMPOSIO_TOOLKIT_VERSION` if you want
  reproducible behavior across Gmail API changes). Still worth a supervised
  first end-to-end run -- actually connecting a Gmail account through the
  generated link and confirming `_production_email_connection_poll_loop`
  picks up the ACTIVE status and texts the confirmation -- before trusting
  it fully unattended. Gmail only for now, per your ask -- Outlook would
  need its own action slugs and auth config (see `email_tools.py`'s module
  docstring for where).
- No WhatsApp (per your call -- Sendblue doesn't support it; dropped for
  now, add a provider like Meta's WhatsApp Cloud API later if you want it
  back).

## Messa's own inbox: `<name>@textmessa.com`, free to start (Cloudflare + Resend)

This is a different email system from the one above: "Real Gmail OAuth
connections" lets Messa use the user's OWN existing Gmail. This section
gives every user a brand-new address Messa owns outright --
`<local-part>@textmessa.com` -- with no OAuth at all, meant for handing out
anywhere (signups, forms, a plumber's contact form) without exposing a
real personal inbox. You picked this over the AgentMail-style "inbox API"
option specifically to keep costs at zero while bootstrapped: Cloudflare
Email Routing is free for inbound, Resend has a free tier for outbound.

**What ships in code, ready to wire up:**

- `migrations/013_personal_email.sql` -- adds `users.messa_email_local_part`
  (unique). Its `inbound_personal_emails` dedup table is now **legacy/
  unused**: migration 014 below superseded it for dedup, and nothing writes
  to it anymore. Left in place untouched (a few already-tested rows from
  before 014 shipped) rather than dropped, per this project's additive-only
  migration philosophy.
- `migrations/014_messa_email_messages.sql` -- the real thread log now:
  one `messa_email_messages` table holding BOTH directions (`direction`
  `'inbound'`/`'outbound'`), keyed for threading by `thread_id` (computed
  from the RFC 5322 `References`/`In-Reply-To` convention -- see
  `db._compute_thread_id`), deduped on inbound by a `UNIQUE` constraint on
  `message_id`, and carrying a `sent_autonomously` flag on outbound rows
  (see the autonomy policy below).
- Every user gets an address automatically, no onboarding question needed --
  `db.get_or_create_messa_email_local_part` runs on every context load (same
  "cheap after the first time" shape as the live-view share token), slugifying
  their name (`"Jane Doe"` -> `jane`) and falling back to a
  collision-proof `jane482`-style suffix only if the clean slug is already
  taken.
- `messa/channels/resend.py` -- outbound send. Generates its own
  `Message-ID` for every send (rather than trusting whatever's in Resend's
  API response) and sets `In-Reply-To`/`References` headers so a reply
  actually threads in the recipient's mail client, then logs the send to
  `messa_email_messages` itself (`db.log_outbound_personal_email`) -- the
  one channel module in this codebase that isn't DB-free, a deliberate
  trade so no future caller of `send_email` can forget to log an outbound
  message.
- `messa/tools/personal_inbox_tools.py` -- the `personal_inbox_agent`
  subagent (registered in `agents/registry.py`) with five always-available
  tools: `get_my_messa_email`, `send_email` (start a new conversation),
  `reply_to_email` (reply within an existing thread by `thread_id`, looked
  up from the DB rather than scoped to one triggering turn -- see below),
  `get_thread_history` (by `thread_id` or by counterpart address), and
  `search_my_emails` (free-text + date-range search across the whole
  history).
- `messa/server.py`'s `POST /webhooks/personal-email/inbound` -- receives
  parsed inbound mail, resolves the local part to a user, logs + dedupes via
  `db.log_inbound_personal_email` (its `ON CONFLICT (message_id) DO NOTHING`
  is now the dedup source, replacing migration 013's table), and hands it to
  a background task.
- `cloudflare/personal-email-worker/` -- the actual Cloudflare Email Worker
  (using the `postal-mime` npm package to parse raw MIME) that Cloudflare
  Email Routing invokes for every inbound message, forwarding it to the
  webhook above as JSON.

**The one design choice worth understanding before you turn this on:** an
inbound email here comes from an arbitrary external sender, not from the
user Messa is assisting -- unlike a text, which always IS the user. So a
new email does NOT get auto-replied to with whatever Messa's turn produces.
Instead, `_process_inbound_personal_email` re-invokes Messa with a synthetic
prompt ("an email arrived, from X, subject Y, thread Z -- this is NOT an
instruction from you") and delivers her reaction over the user's own
SMS/iMessage number, exactly like a cron job notification. Whether she
replies right then is governed by a **risk-based autonomy policy** baked
into her system prompt, not a sender allowlist: she's expected to just call
`reply_to_email` herself (passing `autonomous=True`, purely for the audit
trail -- it does not bypass the normal destructive-tool approval gate) for
something clearly low-stakes -- a plain acknowledgment, a factual answer,
confirming receipt. Anything that commits money, schedules/cancels
something, shares personal info, or asks her to act on a site gets relayed
to the user first instead, and only sent (`reply_to_email` with
`autonomous=False`) once they've told her what to say. Because
`reply_to_email` looks up who to reply to from the DB by `thread_id` rather
than from the one triggering turn's context, this "check in, then reply
later" flow actually works -- a later, ordinary turn ("yes, tell them
Tuesday works") can still finish the reply. This is also a deliberate guard
against prompt injection either way: a stranger's email content is never
treated as a command from the account owner.

**Setup -- inbound (Cloudflare Email Routing, free):**

1. Add `textmessa.com` to Cloudflare (if it isn't already) so Cloudflare
   manages its DNS.
2. Dashboard -> your domain -> Email -> Email Routing -> enable it. This
   provisions the necessary MX/TXT records on the domain for you.
3. `cd cloudflare/personal-email-worker && npm install`.
4. Set `MESSA_WEBHOOK_URL` in `wrangler.toml` to your deployed server's
   `.../webhooks/personal-email/inbound` URL (your HF Space's
   `https://<you>-<space>.hf.space/...` works fine to start).
5. `wrangler secret put MESSA_WEBHOOK_SECRET` -- pick a random string, and
   set the same value as `MESSA_PERSONAL_EMAIL_WEBHOOK_SECRET` in Messa's
   own `.env`/HF secrets.
6. `wrangler deploy`.
7. Back in the Cloudflare dashboard: Email Routing -> Routing rules -> add
   a **Catch-all address** rule -> action "Send to a Worker" -> pick
   `messa-personal-email`. This is what makes every `<anything>@textmessa.com`
   reach the Worker, not just addresses you'd have to register one by one.

**Setup -- outbound (Resend, free tier):**

1. Create a Resend account, add `textmessa.com` as a sending domain.
2. Add the DKIM/SPF (and recommended DMARC) DNS records Resend's dashboard
   gives you -- also on Cloudflare's DNS tab for the domain, same place as
   step 1 above. Domain verification usually clears within a few minutes to
   an hour.
3. Create an API key, set it as `RESEND_API_KEY` in `.env`/HF secrets.

Until `RESEND_API_KEY` is set, `send_email`/`reply_to_email` just tell
Messa plainly that sending isn't configured yet -- addresses are still
assigned and inbound mail still reaches the user over SMS either way, so
inbound-only is a perfectly reasonable way to try this before turning on
outbound.

**A gap worth knowing about, not yet closed:** nothing here rate-limits or
sanity-checks the size/frequency of inbound mail per user beyond the
`Message-ID` dedup -- a mailing list someone signs the user's Messa address
up for would currently trigger a full agent turn (and an SMS) per message.
Fine at hobby scale; worth a per-user rate limit or digest-instead-of-per-message
mode before this is handed to real users at any volume.

**Still needs a real-account check, not verified from this sandbox:**
`channels/resend.py` sets its own generated `Message-ID` as an explicit
outbound header rather than trusting Resend's API response, because
`thread_id` computation depends on knowing a message's own `Message-ID` up
front. Some providers silently override a custom `Message-ID` header for
their own deliverability tracking -- worth one real supervised test after
inbound/outbound are both live: send a reply, then confirm the recipient's
own follow-up reply's `In-Reply-To` actually matches what got logged (query
`messa_email_messages`, or ask Messa via `get_thread_history`), i.e. that
threading survives a real Resend round-trip end to end.

**Verification.** Send a test email to a real provisioned address once both
setup sections above are done, and confirm: the Worker's `wrangler tail`
shows the parse+forward succeeding, the webhook logs "accepted" (not
"unknown recipient" -- check the local part matches what
`get_my_messa_email` reports for that user), and the user's phone gets a
text describing the email within a few seconds. Once `RESEND_API_KEY` is
set, also confirm a `reply_to_email`/`send_email` round-trip: the recipient
actually receives it, and `get_thread_history`/`search_my_emails` show both
sides of the conversation afterward. No live Cloudflare/Resend account
exists in this sandbox, so none of the three setup steps above (DNS
propagation, the Worker actually receiving real mail, Resend's domain
verification) -- nor the Message-ID round-trip check above -- could be run
end-to-end here; this is the one part of this feature that needs a
supervised first real run, same caveat as every other "needs a live account
this sandbox doesn't have" feature above.

## Default email provider, and email_agent rebuilt as a small-loop CompiledSubAgent

Two related asks. First: with two separate inboxes now real (the Messa-owned
address above, and the user's own Gmail via Composio -- see "Real Gmail OAuth
connections" further up), a plain "send an email" or "check my email" is
ambiguous unless the user always names which one they mean. Second: `email_agent`
had never gotten the same tuning pass `executive_assistant` got (see "Executive
assistant rebuild" above) -- it was still a plain declarative `SubAgent` dict
with a static system prompt, silently inheriting whatever step budget happened
to be ambient on the call (100, deepsearch-sized) instead of one sized to what
it actually does.

**Default provider (migrations/015_default_email_provider.sql).** `users.default_email_provider`
is a new `'messa' | 'gmail'` enum column, `NOT NULL DEFAULT 'messa'` -- which does
double duty on purpose, per your explicit ask ("even for existing users now,
make messa's email the default, and the same for anyone who onboards from now
on"): Postgres backfills every *existing* row to `'messa'` as part of the `ADD
COLUMN` itself (fast, metadata-only on PG 11+, no separate `UPDATE` needed), and
every row created *afterward* also starts at `'messa'` since the column's own
DEFAULT applies with no code change to `get_or_create_user`'s plain INSERT. Both
halves of the ask fall out of one clause. `config.UserContext.default_email_provider`
mirrors the same default in Python (`"messa"`), so behavior is identical whether
or not the migration has actually run yet.

The ONLY thing that ever changes it afterward is the user's own explicit request
-- Messa's new `set_default_email_provider` tool (agents/registry.py), called
only when the user says something like "use my gmail as default from now on" or
"switch back to my messa email". **Connecting Gmail does NOT flip this by
itself** -- same "the user decides, nothing switches automatically" shape as
this project's card/top-up decision earlier on. Switching *to* `'gmail'` is
refused (no DB write at all) if Gmail isn't connected yet -- the tool tells
Messa so, and to offer `request_email_connection` first.

Messa's own system prompt (`agents/registry.py`'s `_build_system_prompt`) is
where the actual ROUTING decision happens, in a new "Email routing" paragraph:
a generic request that doesn't name an inbox goes to whichever subagent matches
the user's current default; an explicit "my gmail" / "my messa email" always
overrides the default regardless of what it's set to. The "known about this
user" block also states the current default plainly, including one edge case:
if the default is `'gmail'` but Gmail isn't connected right now (e.g. they
disconnected it, or it expired), Messa is told to treat `personal_inbox_agent`
as the *effective* default until it's reconnected, rather than routing generic
requests into a dead end.

**email_agent rebuilt as a `CompiledSubAgent`.** Exactly the same reasoning
`tools/executive_tools.py`'s `build_executive_subagent` already established:
a plain declarative `SubAgent` dict has no field for its own step budget, so it
silently inherits whatever `recursion_limit` happens to be ambient on the
call -- `config.RECURSION_LIMIT` (100 by default), the same research-sized
budget deepsearch uses, for a role that's normally a handful of direct Composio
calls (list, get, send, reply, or the connect-link flow). `tools/email_tools.py`'s
new `build_email_subagent` wraps the existing `build_email_tools` in a small
inner `create_agent` loop bounded by the new `config.EMAIL_RECURSION_LIMIT`
(12, deliberately smaller than executive's 14 -- Gmail actions are even more
atomic). Unlike executive_assistant, there's no deterministic post-hoc quality
check afterward -- every read/send tool already checks `user.email_connected`
itself and reports a correct, plain message when it isn't (there's no
equivalent "past-due date" class of silent mistake here to catch), so this is
a single `ainvoke`, no checkpointer/nudge-retry machinery needed. The shared
"grab the model's last real reply" logic (`_last_ai_text`) moved out of
`executive_tools.py` into `tools/common.py` (`last_ai_text`) so both
`CompiledSubAgent`s use the exact same extraction instead of two copies of the
same few lines.

`EMAIL_SYSTEM_PROMPT` (a static module constant before) is now
`_build_system_prompt(user)`, a function of the user -- it states plainly
whether Gmail is connected right now, and whether Gmail is currently the
user's default inbox (purely for phrasing, e.g. so it doesn't call itself "the
default" when it isn't) -- plus new guidance on using the message/thread id a
prior `list_recent_emails`/`get_email` call actually returned, rather than one
invented on the spot, and specifically that `reply_to_email` wants the THREAD
id, not a single message's own id. `personal_inbox_agent`'s system prompt
(`build_personal_inbox_system_prompt`, also now a function of `user` rather
than a static `PERSONAL_INBOX_SYSTEM_PROMPT` constant) got the same small,
symmetric addition, purely for phrasing -- the actual routing decision always
happens one level up, in Messa's own prompt, before either subagent is ever
invoked.

**Verification.** All of this is testable without a live account (the fake
Composio client from "Real Gmail OAuth connections" above, plus a `FakeModel`
driving the real `langchain.agents.create_agent`/LangGraph stack, same
technique `test_executive_quality_check.py` already established): `EMAIL_RECURSION_LIMIT`
is confirmed smaller than both `DEEPSEARCH_MAX_STEPS` and the ambient
`RECURSION_LIMIT` a plain `SubAgent` dict would otherwise inherit; a full
list -> send -> finalize round trip runs through the real `CompiledSubAgent`
plumbing with the destructive `send_email` call genuinely going through the
approval gate; and the dynamic system prompt is confirmed to actually change
per connection/default state. Separately: `set_default_email_provider` is
confirmed to refuse switching to Gmail while disconnected (no DB write
attempted), succeed once connected, work case-insensitively, and report a
friendly message pre-migration instead of crashing; `cli._context_from_row` is
confirmed to propagate a real column value and to fall back to `"messa"` for
both a missing key and an explicit `NULL` (a row saved before this migration
existed). What's NOT verified here, same caveat as the rest of this file: an
actual live run where a real user says "use my gmail as default" and a
subsequent "send an email" actually reaches Gmail instead of the Messa
address.

## PDF attachments + a real From name on Messa's own email, and a new Emails page on the live-view dashboard

Three asks, all scoped to `personal_inbox_agent` (Messa's own address on
`TEXTMESSA_EMAIL_DOMAIN`) -- **deliberately NOT** `email_tools.py`/Gmail. When
this scope came up mid-request I asked directly rather than guessing (the
tradeoff: Composio's exact Gmail attachment parameter name isn't
discoverable from public docs or local package inspection without a live
account -- it's only in the tool's live schema, fetched from Composio's API
at runtime -- so a wrong guess there risks a broken send to a real inbox).
The answer was explicit: "Just Messa's own email" -- Resend's attachment API
*is* fully documented, so that's the one this phase actually builds.

**From display name** (`config.UserContext.messa_display_name`, a new
property): every send/reply from Messa's own address now carries a real
display name -- `"Messa, personal assistant of {user.name}"`, or `"Messa,
your personal assistant"` for the rare case a send fires before onboarding
has collected a name -- so a recipient's mail client shows who's actually
writing instead of a bare `jane@textmessa.com`. `channels/resend.py`'s
`send_email` gained an optional `from_name` parameter that builds Resend's
`from` field as `"{from_name} <{address}>"` (bare address, unchanged, when
omitted); `tools/personal_inbox_tools.py`'s `send_email`/`reply_to_email`
tools pass `user.messa_display_name` on every call automatically -- Messa
never signs emails herself or mentions this to the user, it just happens.

**PDF attachments** (migrations/016_messa_email_attachment.sql): the
existing `document_agent` (`tools/document_tools.py`'s `generate_pdf`) always
returned a file path with a comment noting "a later phase will deliver it
over text/email" -- this is that phase. `send_email`/`reply_to_email` both
gained an `attachment_path` parameter; the orchestrator's system prompt
(`agents/registry.py`) now tells Messa to delegate to `document_agent`
first, take the *exact* path it reports back, and relay it verbatim into a
`personal_inbox_agent` delegation -- never invent or paraphrase a path.
`channels/resend.py` is the actual last line of defense on that path, not
just the prompt: `_validate_attachment_path` checks existence, that it's a
real file (not a directory), that it resolves to somewhere *inside*
`config.OUTPUTS_DIR` (so a model-invented path can never make the server
read and mail out an arbitrary file), and the raw size against
`config.MAX_EMAIL_ATTACHMENT_BYTES` (25MB raw by default -- Resend's own hard
cap is 40MB *after* base64 encoding, which inflates size by ~33%, plus
headers/body, so this leaves real headroom rather than cutting it close).
A validated file is base64-encoded into Resend's documented
`{"attachments": [{"filename", "content"}]}` shape. The attachment's
filename (not the path -- the file itself already left the server) is
recorded on `messa_email_messages.attachment_filename`
(migrations/016, additive, nullable, `NULL` for every inbound row and for
any outbound row with nothing attached) via
`db.log_outbound_personal_email`'s new `attachment_filename` parameter --
guarded by its own `_has_column` check so a deployment that hasn't run
migration 016 yet degrades gracefully (the send still succeeds and still
gets logged, just without recording a filename) rather than erroring on an
already-successful send.

**The Emails page** (third toggle-able page on `/live/<token>`, alongside
Live Browsing and Dashboard): a mail-app-style Inbox/Sent list with
click-to-open full-thread view of Messa's own address, requested as "any
other features you think would look good" left open to me too -- what I
added beyond the literal ask: a free-text search box (backed by
`search_personal_emails`'s existing `query` param), a "Load more" button
instead of fixed pagination, and the user's own Messa address shown at the
top of the page.

- `db.search_personal_emails` gained `direction` (`'inbound'`/`'outbound'`/
  `None` for both) and `offset` parameters -- both optional, both default to
  the exact no-filter/no-offset behavior every existing caller (the
  `search_my_emails` tool, and this file's own tests) already relied on.
- Two new routes on `server.py`, same "resolve the token, 404 if unknown"
  contract as the existing `/live/<token>/dashboard` route (reusing the
  same permanent `live_share_token`, not a new kind of link):
  `GET /live/<token>/emails` (query params `direction`, `q`, `limit`,
  `offset` -- `limit` is capped server-side at `EMAILS_PAGE_SIZE_MAX`
  regardless of what's requested) returns the list plus the user's own
  `messa_email` address and a `has_more` flag (computed by fetching one
  extra row and trimming it off, rather than a separate `COUNT` query); and
  `GET /live/<token>/emails/thread` (thread id as a **query param**, not a
  path segment -- a real RFC 5322 Message-ID contains `<`, `>`, `@`, which
  are awkward/unsafe to reliably URL-path-encode but fine as an ordinary
  query string value) returns the full chronological chain via the
  already-existing `db.get_thread_messages`. An unknown `thread_id` on a
  *valid* token returns 200 with an empty list, not a 404 -- only an
  invalid token itself is a 404, same "a bad link never quietly looks
  valid, but a valid link never looks broken either" reasoning the rest of
  this file already follows.
- `live_view_page.py`'s `showPage(name)` (previously hardcoded to exactly
  two pages) now handles three; the new page reuses the file's existing
  `--card-bg`/`--border`/`--radius`/`--shadow`/`--accent`/`--muted` theme
  variables throughout (no new variables introduced), and its Inbox/Sent
  tabs reuse the same `.page-toggle`/`.toggle-btn` segmented-control markup
  the Live Browsing/Dashboard/Emails nav itself uses, so both the
  "terminal" and "polished" skins apply automatically with zero page-specific
  styling work. An autonomous-send badge reads `sent_autonomously`
  (migrations/014) and a paperclip badge reads `attachment_filename`
  (migrations/016) on both the list rows and the thread view. Per this
  file's own module docstring (the security note that's governed every
  other line of JS in it): subject/body/address/snippet text can originate
  from an arbitrary external sender, so every one of those fields is
  written with `textContent`, never `innerHTML` -- verified by extracting
  the rendered `<script>` block and running it through `node --check` (the
  file has no build step to run a real bundler/linter through), and by
  grepping the new JS for the pattern this file's other tests already use
  to catch a stray `innerHTML` on untrusted content.
- The list and thread views both poll on the same cadence as the dashboard
  page (`DASHBOARD_POLL_MS`, 30s) -- a background poll only ever replaces
  the list/thread it's currently *not* actively displaying against the
  other (checked via `emailsShownState`), and the list's shell (toolbar +
  search box) is only ever built once and left alone on refresh, so a poll
  tick can never steal focus out of the search input mid-type, matching
  `ensureDashboardGridShown`'s existing "flip state, don't rebuild" pattern
  in the dashboard page right next to it.

**Verification.** `channels/resend.py`'s own path validation (missing file,
a directory instead of a file, a path escaping `OUTPUTS_DIR`, over the size
cap -- each confirmed to raise *before* any network call happens), the
`from`-field construction (bare address vs `"{name} <addr>"`), a real
attachment's base64 round-trip and its filename reaching the DB log call,
`UserContext.messa_display_name`'s named/nameless fallback, and
`personal_inbox_tools.py`'s `send_email` tool actually passing both
`from_name` and `attachment_path` through unmodified, are all covered
against a faked `httpx.AsyncClient` (no real Resend account touched) in a
new `/tmp/test_email_attachment_and_display_name.py`. The existing
`/tmp/test_personal_inbox.py` fake DB connection was extended for the new
`attachment_filename` column (including its own pre-migration-016
degrade-gracefully path) and for `search_personal_emails`' new
`direction`/`offset` parameters, with new assertions for both. The two new
`server.py` routes are covered end-to-end via FastAPI's `TestClient` against
a monkeypatched `db` (same technique as the existing
`/tmp/test_dashboard_route.py`) in a new
`/tmp/test_emails_dashboard_route.py`: 404-for-unknown-token on both routes,
`direction=inbox`/`sent` mapping to `'inbound'`/`'outbound'`, the
capped-server-side limit, the `has_more` pagination signal computed both
true and false, an unrecognized `direction` value falling back to no filter
instead of a 500, a valid token with an unknown `thread_id` returning 200
with an empty list rather than a 404, and that autonomous/attachment fields
and raw subject/body text survive the round trip byte-for-byte (no
server-side escaping -- that responsibility belongs entirely to the page's
own `textContent`-only JS, per this file's established security model).
`live_view_page.py`'s full rendered template (all three pages) was rendered
and its `<script>`/`<style>` blocks checked for balanced braces and valid
JS syntax via `node --check`. **Not verified here**, same caveat as
everywhere else in this file: an actual Resend account sending a real PDF to
a real inbox (does every mail client actually show the custom From name the
way it's expected to; does the attachment arrive intact), and a real
browser click-through of the new Emails page's UI (only its generated
markup/JS/CSS were checked, not a live rendered DOM interaction the way
`test_dashboard_toggle.py` does for the Dashboard page).

Also worth noting, found but deliberately not touched in this phase:
`/tmp/test_dashboard_route.py`'s `part3_full_route` hardcodes absolute
calendar dates (the week of Aug 24-30, 2026) instead of passing an explicit
`now` into `_week_bounds_utc` the way `part1_week_bounds` correctly does --
so it silently starts failing once the real calendar moves past that week
(confirmed: it fails as of Aug 31, 2026, a pre-existing test-fixture
expiry unrelated to anything in this phase, not a regression from it).

## Four fixes from live testing: threaded Inbox/Sent, a real weekly-schedule navigator, contacts with phone/email, and reminders that actually clear themselves

All four are direct product feedback after trying the previous phase's
Emails page and Dashboard live.

**Emails page: one row per thread, not per message.** The previous
implementation listed individual `messa_email_messages` rows filtered by
`direction` -- a single back-and-forth conversation showed up as separate
fragments scattered across Inbox and Sent, which read as confusing next to
how a real mail app behaves. `db.list_email_threads` (new, alongside the
still-used-elsewhere `search_personal_emails`) replaces it for the list
view: one row per `thread_id`, always previewing that thread's own LATEST
message regardless of that message's own direction, computed in a single
query via `ROW_NUMBER() OVER (PARTITION BY thread_id ORDER BY created_at
DESC)` after first narrowing to qualifying threads through membership
subqueries (so filtering never accidentally excludes an earlier message
from a thread that still qualifies). `direction` here means "Inbox = any
thread with at least one inbound message, Sent = any thread with at least
one outbound message" -- NOT "only inbound/outbound messages" -- so a
thread you've replied to legitimately shows up under both tabs, each time
previewing the exact same latest message, exactly like Gmail keeps a
conversation visible under both Inbox and Sent once there's been a reply.
`query` now matches against ANY message in the thread, not just the
previewed one, so a thread is still findable by something said earlier in
it. `server.py`'s `/live/<token>/emails` route just swapped which db
function it calls -- the JSON shape, the thread-detail route
(`/live/<token>/emails/thread`, already always returning the FULL
chronological thread regardless of direction), and every bit of the
page's own JS were already correct and needed no changes at all; the
fragmentation was purely a list-query problem.

**Dashboard schedule: working prev/next/Today navigation.** The schedule
card was permanently locked to "this week," computed fresh every poll with
no way to look at any other week. `server.py`'s `/live/<token>/dashboard`
route now takes a `week_offset` query param (weeks forward/back from the
real current week, clamped to `+/-DASHBOARD_WEEK_OFFSET_MAX` so a
malformed/huge value can't force a query for a wildly distant week) and
shifts `_week_bounds_utc`'s own `now` argument by that many weeks before
computing the range -- `is_today` on each day is still computed against
the ACTUAL current date regardless of `week_offset`, so it only ever lights
up on a day that's really today, never claims some other week's matching
weekday is "today." The response now also echoes back the `week_offset` it
actually honored (post-clamp), so the client can stay in sync even if it
requested something out of range. On the page itself, the schedule card
builds its own header now (instead of `dashCard`'s plain-text one) with
"‹"/"›" buttons plus a "Today" button that only appears once you've
navigated away from the current week -- clicking any of them updates a
small `dashWeekOffset` client-side variable and refetches immediately
(factored `fetchDashboard()` out of the polling loop so both the 30s
background poll and an explicit user click share the same fetch/render
path) rather than waiting for the next background poll tick. Reuses the
existing theme variables throughout, same as everything else on this page.

**Contacts can now carry a phone number and email.** The `people` table
(part of the original base schema, `neon-schema.sql` -- itself never
touched again after initial creation, per this project's additive-only
migration philosophy; all evolution since has lived entirely in
`migrations/*.sql`) only ever had name/relationship/notes.
`migrations/017_contacts_phone_email.sql` adds nullable `phone_number`/
`email` columns; `db.upsert_person` gained matching parameters (guarded by
`_has_column`, so a deployment that hasn't run migration 017 yet still
saves what it already understood instead of erroring on the whole call),
and a new `db.find_person_by_name` does a case-insensitive partial-name
lookup returning every match (not just the first), so an ambiguous name
can be flagged back to the user rather than silently guessing wrong. This
is what actually closes the loop on "Messa can access it when the user
specifies a name and sends email": a new orchestrator-level DIRECT tool
(`find_contact`, `agents/registry.py`) -- a plain lookup, not a
delegation, same "cheap enough not to need a whole subagent round trip"
reasoning as the existing `web_search`/`fetch_page_text` direct tools --
calls `find_person_by_name` and formats the result. The main system
prompt now tells Messa to call `find_contact` FIRST whenever the user
names someone by name for an email or text without also giving an actual
address/number ("email Sam about the invoice"), ask which one if it finds
more than one match, and ask the user directly (never guess or invent an
address) if it finds none or finds the contact but not the info actually
needed. `executive_tools.py`'s existing `upsert_contact`/`list_contacts`
tools got the matching parameter/formatting updates (a shared
`_format_contact_line` helper, reused by `find_contact` too so the two
surfaces describe a contact identically), and the dashboard's own Contacts
card now shows phone/email in its meta line as well.

**Reminders actually clear themselves once delivered.** `db.
mark_reminder_sent` used to just flip a reminder's `status` to `'sent'` --
technically excluded from the dashboard's own `status='pending'` query and
`executive_tools.py`'s default `list_reminders()` call, but the row itself
stuck around indefinitely, which read as confusing leftover clutter rather
than something that had cleanly finished its job. Per explicit product
decision, it now `DELETE`s the row outright the moment delivery succeeds
(`server.py`'s `_production_reminder_loop`, and `background.py`'s
CLI-only preview poller, both already called this same function -- neither
needed to change). `reminder_status`'s `'sent'` enum value is left defined
(an enum value already shipped is never dropped) but nothing writes it
going forward; a reminder that's somehow already gone by the time this
runs is a silent no-op, same as every other id-keyed delete already in
this file.

**Verification.** `db.list_email_threads`' actual grouping/direction-
membership/search logic (not just the route wrapped around it) is covered
against a small fake connection that models a realistic multi-thread,
mixed-direction inbox in a new `/tmp/test_emails_dashboard_route.py` part:
a thread with both directions previews its true latest message and
correctly appears under both Inbox and Sent with the identical preview
either way; an inbound-only thread is Inbox-only; an outbound-only thread
is Sent-only; search matches an older message in a thread; pagination
pages over threads, not raw rows; and it no-ops pre-migration. The same
file's route-level part (now pointed at `list_email_threads` instead of
the old `search_personal_emails`) and a new part for the dashboard's
`week_offset` (defaults to 0, round-trips in the response, shifts the
requested date range by exactly one week per step in either direction,
and is clamped server-side against an absurd value) both pass, and
`test_dashboard_toggle.py`'s real-rendered-DOM check confirms the new
"‹"/"›" controls actually render inside the schedule card's own heading.
A new `/tmp/test_reminder_autodelete_and_contacts.py` covers
`mark_reminder_sent`'s delete behavior (including the already-gone-id
no-op case), `upsert_person`'s new fields (including that a later update
with no phone/email given never clobbers what's already saved, and the
pre-migration-017 degrade-gracefully path), `find_person_by_name`'s
case-insensitive partial matching, and the `find_contact` tool end to end
(an ambiguous two-match case, a specific single match, and a no-match
case). **Not verified here**, same caveat as always: a real click-through
of the week-nav buttons and the Emails page's thread-grouped list against
a live rendered browser DOM (`test_dashboard_toggle.py` covers the
schedule card's markup rendering, not a real click on the new buttons),
and a real end-to-end "email Sam" request against a live model actually
calling `find_contact` unprompted in the middle of a real conversation.

## PDF support, round two: reading a PDF someone sends Messa, and texting one back out

The earlier PDF-attachment phase only ever covered outbound email
(`document_agent` -> `personal_inbox_agent`'s `attachment_path` ->
`channels/resend.py`). This phase closes the other three PDF gaps
explicitly asked for: (1) a PDF texted to Messa over SMS/iMessage, (2) a
PDF attached to inbound mail on her own address, and (3) Messa texting a
PDF back out, not just emailing one. All three share one new module,
`messa/pdf_reader.py`, and one new config knob, `config.MAX_PDF_READ_PAGES`
(default 10) -- explicitly "easily modified by a variable when needed," per
the ask, following the exact same `os.environ.get(...)` pattern every other
tunable in this project already uses. It applies ONLY to reading: generating
a PDF (`document_tools.py`'s `generate_pdf`) stays unlimited, unchanged.
`config.MAX_PDF_READ_BYTES` (default 20MB) caps the raw file size before
any parsing is even attempted, on top of the page cap.

**`messa/pdf_reader.py`: one extraction function, used by every inbound-PDF
path.** `extract_pdf_text(data: bytes, *, max_pages=None) ->
(text, pages_read, truncated)` opens the bytes with `pypdf` (pure Python,
no system libraries -- same reasoning `document_tools.py` already documents
for choosing `reportlab` over a `weasyprint`/HTML pipeline: this has to
keep working unmodified on HuggingFace Spaces' CPU-basic Docker image),
makes one cheap attempt at an empty-password decrypt (some "encrypted"
PDFs are just that, not actually protected), reads up to the page limit,
and joins each page's extracted text. `truncated` is `True` whenever the
real PDF had more pages than the limit allowed -- both callers below fold
that into a note so "read the first 10 pages of a 40-page PDF" is never
silently presented as "read the whole document." A `PdfReadFailure`
(never a raw `pypdf` exception) covers every failure mode in one place: a
corrupt file, a real password, or a scanned/image-only PDF with no
extractable text at all (OCR is out of scope).

**Sub-request 1: texting Messa a PDF.** Sendblue's inbound webhook already
carries `media_url` (a CDN link to any MMS/iMessage attachment) alongside
`content` -- confirmed against Sendblue's own docs, since this project has
no live account to test the real payload shape against. Previously,
`sendblue_webhook` silently dropped any message where `content` was empty,
which meant a captionless "just the PDF, no text" MMS was never even
accepted. It now accepts the webhook when EITHER `content` or `media_url`
is present, and `_process_inbound` gained a `media_url` parameter: a new
`_maybe_read_inbound_pdf(media_url)` downloads the attachment and, ONLY if
it actually looks like a PDF (`Content-Type` header or the `%PDF-` magic
bytes -- an ordinary photo MMS just flows through completely unchanged, no
PDF note appended, exactly like before this shipped), runs it through
`pdf_reader.extract_pdf_text` and returns a ready-to-inject text block
(success with the extracted text, "too large," or "couldn't be read").
That block gets appended to whatever caption text came with the message
before the normal turn runs; if there's still nothing to act on (a
captionless non-PDF attachment), the function returns early without ever
calling `run_message` -- the same "silently ignored" behavior a captionless
photo MMS already had, now reachable via a different code path instead of
being blocked at the webhook's own door. Messa's own system prompt tells
her the extracted text is already sitting right in the message that told
her a PDF arrived -- she doesn't fetch or open anything herself.

**Sub-request 2: a PDF attached to inbound mail on Messa's own address.**
`cloudflare/personal-email-worker/worker.js` already parses full MIME via
`postal-mime`; it now calls `PostalMime.parse(message.raw, {attachmentEncoding:
"base64"})` (letting the library hand back attachment content as a
ready-to-forward base64 string instead of the default `ArrayBuffer`,
rather than hand-rolling that conversion), filters `parsed.attachments` down
to PDF-shaped ones (by `mimeType` or a `.pdf` filename) under a
15MB-base64-chars ceiling (`MAX_FORWARDED_ATTACHMENT_BASE64_CHARS` -- a
separate, Worker-side-only cap purely so a huge attachment doesn't inflate
the JSON payload with something that's going to get rejected server-side
anyway), and adds a `pdf_attachments: [{filename, content_base64}, ...]`
array to the JSON payload it already POSTs to
`/webhooks/personal-email/inbound`. Server-side, that route decodes and
extracts text from the FIRST PDF attachment (matches the "she should be
able to read it" scope this shipped for -- a message with several PDFs
isn't the common case), builds a `pdf_note` string the same shape as the
SMS path's, and -- critically -- never persists the raw base64 blob: the
JSON stored in `raw_json` is sanitized down to just the filename before
the `INSERT`, so a large attachment never bloats that column. The extracted
text itself isn't persisted anywhere either; it's handed straight to
`_process_inbound_personal_email` as a new `pdf_note` parameter and folded
into the synthetic prompt Messa sees for that inbound email, the same
"available in the moment, not retrievable from history later" tradeoff the
SMS path makes. `attachment_filename` (`migrations/016`'s column,
previously written only by outbound sends) is now also set on the inbound
row when a PDF was found, guarded by the same `_has_column` check
`log_outbound_personal_email` already used -- so the paperclip note in
`_format_message_line`/the live-view Emails page now shows up for inbound
mail too.

**Sub-request 3: Messa texting a PDF back out.** Sendblue's `media_url`
has to be a URL its own servers can fetch -- not raw bytes, unlike outbound
email, where `channels/resend.py` just base64-encodes the file straight
into the request. That's the one new piece of infrastructure this needed:
`migrations/018_generated_document_shares.sql` (a `generated_document_shares`
table -- `user_id, token UNIQUE, file_path, filename`), `db.create_document_share`
/ `get_document_share_by_token`, and a new public `GET /files/{token}`
route in `server.py`. The token is generated the same way as
`users.live_share_token` (`secrets.token_urlsafe(24)` -- unguessable, not
enumerable), and the route re-validates `file_path` against
`config.OUTPUTS_DIR` again at SERVE time, not just trusting it was valid
when the share row was created -- same defense-in-depth posture as the
existing email-attachment path, so even a stale row can never serve a file
that's since moved outside the outputs directory; an unknown token or a
vanished file both 404, never a 500 that might leak a path. The actual
send is a new orchestrator-level DIRECT tool,
`agents/registry.py`'s `send_pdf_over_text(file_path, caption=None)`
(same shape as the just-added `find_contact` tool) -- it validates the
path, mints a share token, builds the `/files/{token}` URL from
`config.LIVE_VIEW_BASE_URL`, and calls `channels/sendblue.py`'s
`send_message(..., media_url=...)` directly, mirroring how
`personal_inbox_tools.py`'s email tools call `channels/resend.py` directly
rather than going through the generic per-turn `send` callback
(`_sms_send_factory` in `server.py`) -- threading a new attachment
parameter through that generic machinery would have been architecturally
heavier than needed for what's fundamentally a one-off direct send to the
user's own number. It goes straight to the user's own phone number, not a
third party, so -- like any other message Messa sends the user directly --
it needs no confirmation step.

**A new shared path-validation function, `config.resolve_output_file`.**
`channels/resend.py`'s `_validate_attachment_path` (exists, is a real
file, lives inside `config.OUTPUTS_DIR`, under a size cap -- the same
directory-traversal defense this project has used since the first
PDF-attachment phase) was private to that one module. It's now factored
into `config.resolve_output_file(path, *, max_bytes=None) -> Path`
(raising `ValueError`), with `resend.py`'s function reduced to a thin
wrapper that just re-raises as `ResendError` so existing callers see the
same exception type they always have. `send_pdf_over_text` above uses the
shared function directly (with the new, smaller
`config.MAX_SMS_ATTACHMENT_BYTES` cap -- default 8MB, deliberately much
tighter than email's 25MB: an MMS recipient on the SMS/RCS carrier
fallback, not iMessage over data, is often behind a real carrier size
ceiling of just a few MB, and Sendblue's own CDN re-hosting doesn't change
what the receiving carrier will actually deliver) -- so the one
security-critical "is this path actually safe to read" check now backs
every outbound-attachment path in the project, not just email's.

**New dependency:** `pypdf` (added to `requirements.txt`) -- chosen over
`pdfplumber` (the other library the request suggested) for the same "pure
Python, no system libraries" reasoning `reportlab` was already chosen for:
`pdfplumber` pulls in `pdfminer.six`, a heavier dependency for text
extraction this simple.

**Not extended to Gmail:** all three sub-requests above are scoped to
Messa's own channel/address only -- SMS/iMessage via Sendblue and Messa's
own email via `personal_inbox_agent` -- consistent with the earlier
PDF-attachment phase's explicit scoping (confirmed via `AskUserQuestion` at
the time) that excluded `email_tools.py`'s Gmail-via-Composio path from
attachment support. That same boundary applies here: nothing in this phase
touches Gmail.

**Verification:** a new `/tmp/test_pdf_support.py` (7 parts, all passing,
alongside a full clean re-run of the untouched-but-signature-adjacent
suites -- `test_personal_inbox.py`, `test_email_attachment_and_display_name.py`,
`test_default_email_provider.py`, `test_email_subagent.py`,
`test_dashboard_toggle.py`, `test_executive_quality_check.py`,
`test_executive_tools_direct.py`, `test_emails_dashboard_route.py`,
`test_reminder_autodelete_and_contacts.py` -- all still green, including
`test_personal_inbox.py`'s own fake DB, updated for `log_inbound_personal_email`'s
new `attachment_filename` parameter and a new inbound-with-attachment
INSERT-shape branch it hadn't modeled before): `pdf_reader.extract_pdf_text`
against real `reportlab`-generated PDFs (page-cap truncation, a custom
`max_pages` override, garbage/empty bytes raising `PdfReadFailure` cleanly);
`config.resolve_output_file` (a real file inside `OUTPUTS_DIR`, a
`/etc/passwd`-style escape, a `../` traversal attempt, a missing file, an
oversized file); `db.create_document_share`/`get_document_share_by_token`
round-tripping through a fake connection (plus the pre-migration-018
no-op path); `send_pdf_over_text` end to end against faked `sendblue.send_message`
and `db.create_document_share` (a successful send, a path outside
`OUTPUTS_DIR`, Sendblue not configured, and a `SendblueError` from the
send itself all reported plainly rather than raised); `GET /files/{token}`
via `TestClient` (a valid share serves the real bytes, an unknown token
404s, a share pointing at a since-vanished file 404s rather than 500ing);
`_process_inbound`'s new `media_url` handling via a faked `httpx.AsyncClient`
(a plain text message is unaffected; a captionless PDF MMS still reaches
`run_message`, not silently dropped; a captioned PDF keeps both the
caption and the extracted text; a captionless non-PDF/photo MMS is still
silently ignored, no `run_message` call at all; a captioned photo still
delivers just the caption; an oversized or corrupt PDF attachment produces
a clear note instead of crashing); and `personal_email_inbound_webhook`'s
`pdf_attachments` handling via `TestClient` (a plain email logs with no
`attachment_filename`; a real PDF attachment gets extracted and its text
relayed as `pdf_note` while the base64 blob itself never lands in
`raw_json`; a corrupt attachment still 200s with a plain failure note
instead of a 500; an unknown recipient is unaffected).

**Not verified here**, same caveat as every other phase in this project: a
real Sendblue account actually delivering an MMS with `media_url` pointing
at this project's own `/files/{token}` route (the mechanism -- an external
URL Sendblue's servers fetch -- is confirmed against Sendblue's own docs,
not against a live send); a real inbound MMS webhook payload's exact
`media_url` field shape (confirmed via Sendblue's docs, not a live
account); and the Cloudflare Worker's `postal-mime` attachment parsing
against a real inbound email carrying an actual PDF (`node --check` only
confirms the JS is syntactically valid, not that a live Cloudflare Email
Routing delivery round-trips a real attachment correctly end to end).

## Morning/evening briefings: from a sequential LLM turn per user to a deterministic, parallel-rendered template

Direct product feedback: a morning/evening briefing was just another
`cron_jobs` row (`config.DEFAULT_BRIEFINGS`) whose saved `prompt_or_task`
got handed to a full Messa LLM turn every time it fired
(`server.py`'s `_production_cron_loop`) -- meaning every due user's
briefing was a real model round-trip, run ONE AT A TIME in a `for job in
due:` loop, even though the underlying content (calendar events, tasks
due, reminders, weather) needed no judgment to assemble and read
essentially the same shape every time. This phase replaces that with a
deterministic template renderer that fetches one user's data in parallel
(`asyncio.gather`) and a new delivery loop that fans an entire due batch
out concurrently (a second `asyncio.gather`, over every due user) -- no
model call anywhere in the path. Any OTHER recurring automation a user
actually asks Messa to set up (`routines_agent`'s
`propose_create_recurring_cron`) is untouched: that's genuinely arbitrary,
model-authored text with no fixed shape, so it still goes through a real
LLM turn exactly as before.

**The split, precisely.** `messa/db.py`'s `get_due_cron_jobs_for_delivery`
gained an `exclude_kinds` param; `server.py`'s `_production_cron_loop` now
passes `BRIEFING_KINDS` (a new module constant, `tuple(config.DEFAULT_BRIEFINGS.keys())`
-- derived from the existing config, not a second hardcoded list that
could drift) so the two system-provisioned briefing kinds never reach its
per-job LLM path at all. A new `db.get_due_briefing_jobs_for_delivery`
handles just those two kinds, joined with the extra user profile fields
the deterministic renderer needs (name, latitude/longitude) that an
arbitrary automation never required (it only ever needed `phone_number`).
A new `server.py` loop, `_production_briefing_loop` (added to `_startup`'s
`_bg_tasks` alongside the existing reminder/cron/deepsearch-pause/email-
connection pollers, same polling cadence), fetches the due batch and runs
`asyncio.gather` over a new `_send_one_briefing(job)` per job -- render,
send via Sendblue, and reschedule via `compute_next_run`/
`db.reschedule_cron_job`, all still per-job try/except'd exactly like the
old loop, but no longer serialized behind each other.

**`messa/briefings.py` -- the deterministic renderer.** `render_morning_briefing`/
`render_evening_briefing` each take one job row and fetch everything for
the relevant local day(s) in parallel: `db.list_calendar_events_for_range`,
`db.list_tasks`, `db.list_reminders`, and `messa/weather.py`'s forecast
call (see below). Morning covers today: events, tasks due today or
overdue, and reminders set for today. Evening recaps today (tasks marked
done today, today's events) and previews tomorrow (tomorrow's weather/
events/reminders, plus anything due tomorrow or still overdue) -- the same
semantics the old LLM prompt described in English, just computed
deterministically instead of interpreted by a model each time. Per
explicit product decision (the earlier LLM prompt's "reminders" was
actually never mentioned, but the new request calling out "calendar
events, reminders, tasks and weather" explicitly added it): reminders
shown are scoped to just that day (today for morning, tomorrow for
evening) rather than every pending reminder regardless of date, keeping
the message short even when several are queued out. An empty day still
renders a short, warm fallback line ("Nothing on deck today..." /
"Quiet day...") rather than nothing -- explicit product decision to keep
`render_briefing` NEVER crash or blank out: any single missing piece
(most commonly weather) is just dropped from that day's text, never
blocks the rest. Plain text throughout, matching the same no-markdown/
no-bullets SMS convention `agents/registry.py`'s own system prompt already
enforces for a live model turn on this channel.

**`messa/weather.py` -- the one new external dependency, chosen to add
zero NEW third-party surface.** `timeutil.py`'s existing city/zip geocoding
already calls Open-Meteo's free, keyless API
(`https://geocoding-api.open-meteo.com/v1/search`) -- this reuses the SAME
provider's forecast endpoint (`https://api.open-meteo.com/v1/forecast`,
also free and keyless: confirmed via Open-Meteo's own site during this
phase at up to 10,000 calls/day on the non-commercial free tier,
comfortably enough for two briefings/day/user at any realistic user
count), so the project has exactly one weather-adjacent dependency to
reason about, not two. Requests just the `daily` block
(`weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max`)
for `config.WEATHER_FORECAST_DAYS` (2, covering both briefings from one
fetch) days, in Fahrenheit (`config.WEATHER_TEMPERATURE_UNIT`) and in the
LOCATION's own local calendar (Open-Meteo's `timezone` param) so "today"
never straddles a UTC day boundary depending on what hour this happens to
run. A small WMO-code table (`open-meteo.com/en/docs`' own documented
subset, extended with the standard remaining codes -- 56/57 freezing
drizzle, 66/67 freezing rain, 77 snow grains, 85/86 snow showers -- for
full coverage) turns `weather_code` into a short phrase like "partly
cloudy"; `DayWeather.one_liner()` renders "78°/61°F, partly cloudy" (or
"...,20% rain" when Open-Meteo's own rain-chance clears a 10% floor --
skipping that clause on a near-certain-dry day keeps it shorter).

Explicit product decision, asked for directly ("if our weather is
unreliable its better to drop it"): there is no fallback weather provider
and no partial/best-guess result. `get_daily_weather` returns `None` --
never a raw exception, never a partial list -- on ANY network failure,
timeout, malformed/misaligned response shape, OR an implausible reading on
ANY requested day (`_is_plausible`: outside a generous real-world
temperature range, or a low reported above its own high) -- one bad day in
the response taints the whole thing, since both days came from the same
call and a broken response isn't more trustworthy for today just because
tomorrow's the one that looked wrong. `briefings.py`'s renderer treats
`None` exactly the same as "no coordinates on file at all" (a user who
hasn't given a resolvable city yet): the weather line is silently
dropped, the rest of the briefing renders normally.

**Coordinates: captured once, at the exact place a location is already
being resolved.** `timeutil.py`'s Open-Meteo geocoding response already
carries `latitude`/`longitude` on every result -- previously discarded the
moment a timezone was picked. `TimezoneResolution` now carries them too;
`db.update_user_timezone` gained matching `latitude`/`longitude` params
(written in the SAME `UPDATE` as the timezone, guarded by its own
independent `_has_column` check so a deployment with migration 007 but
not yet 019 still updates the timezone, just without coordinates); both
existing call sites (`db.ensure_timezone_resolved`,
`db.save_profile_field`'s city-save branch) now pass them through. New
additive `migrations/019_user_coordinates.sql`
(`users.latitude`/`users.longitude`, both `DOUBLE PRECISION`, nullable --
`NULL` for anyone who hasn't given a resolvable city yet, or skipped that
onboarding step). No separate geocoding round-trip happens at
briefing-render time -- by the time a briefing fires, the coordinates (if
resolvable at all) were already saved the moment the user gave Messa
their city.

**Not extended:** briefing SEND TIMES are unchanged -- still
`config.DEFAULT_BRIEFINGS`' fixed 7:00 AM / 8:00 PM local cron expressions,
same per-user timezone-aware scheduling `db.ensure_default_briefings`
already provisioned; this phase only changed how a due job gets turned
into text and how a due BATCH gets delivered, not when any individual
user's briefing fires. A user can still cancel/pause their own briefing
via the existing cron-job tools exactly as before (that mechanism didn't
change).

**Verification:** a new `/tmp/test_briefings.py` (4 parts, all passing,
alongside a full clean re-run of `test_timeutil.py` and the whole
previously-existing suite -- nothing regressed): `messa/weather.py`'s WMO
description table, `DayWeather.one_liner`'s rain-clause threshold, and
`get_daily_weather`'s parsing/rejection logic against a faked `httpx`
client (a good 2-day response, a network exception, a response missing
its `daily` key, an implausible reading on one day tainting the whole
result, mismatched array lengths, and a non-2xx status -- all correctly
returning `None`); `get_due_cron_jobs_for_delivery`'s new `exclude_kinds`
param and the new `get_due_briefing_jobs_for_delivery` against a fake
connection (including the pre-migration-011/pre-migration-019 degrade
paths, and confirming a not-yet-due job is correctly excluded);
`update_user_timezone`'s new coordinate params across all four
has-confirmed-column/has-coords-column combinations; `render_morning_briefing`/
`render_evening_briefing` against faked `db.*`/`weather.*` calls (a normal
day's greeting/weather/events/overdue-task-labeling/reminders, weather
dropped when coordinates are missing, weather dropped when
`weather.get_daily_weather` itself returns `None`, the empty-day and
quiet-evening fallback text, a nameless user getting a plain greeting
instead of "Morning, None!", and `render_briefing`'s dispatch including an
unrecognized-kind `None` return); and `server.py`'s `_send_one_briefing`
(render+send+reschedule, a `None` render sending nothing but still
rescheduling, and a Sendblue delivery failure still rescheduling rather
than getting the job stuck) plus confirming both new/changed background
loops actually call the right `db.*` function with the right arguments.
One real bug this test-writing caught before shipping: the first draft of
`render_evening_briefing`'s quiet-day fallback checked the SAME "tomorrow"
bucket that weather also got appended to, so a user with nothing planned
but a valid weather reading would never see the "nothing on the calendar"
line at all -- fixed by tracking schedule content (events/due tasks)
separately from the weather line for exactly that fallback check, while
still showing weather on an otherwise-quiet day (it's useful information
on its own).

**Not verified here**, same caveat as every other phase in this project:
this sandbox's own outbound network is restricted to a small package-
registry allowlist (a live call to `api.open-meteo.com/v1/forecast` from
here returned a `403` from the sandbox's own egress proxy, not from
Open-Meteo -- the same restriction `timeutil.py`'s geocoding tests already
document), so the real forecast endpoint's exact response shape is
confirmed against Open-Meteo's own published docs (fetched during this
phase) rather than a live call from this environment; a real batch of
concurrent Sendblue sends at 7:00 AM against a live account; and a live
user's `latitude`/`longitude` actually landing correctly through the full
onboarding flow (`save_profile_field`'s city branch) against a real Neon
database.

## Onboarding: 3 questions instead of 4, ending with a deterministic reveal of what Messa actually is

The ask: onboarding was asking name, then an optional email, then location,
then a 4th mandatory "want me to connect your Gmail?" yes/no gate before it
would ever reach `onboarding_step = 'complete'` -- four back-and-forth
exchanges before a brand-new user saw anything beyond a form. The request
was to cut it down to exactly 3 questions (name, city/zip, an optional
email "for creating accounts"), and to use the moment onboarding actually
finishes to reveal something concrete: Messa's own email address, framed as
hers to manage and safe to hand out anywhere, plus the live-view link,
plus a clear statement that Messa is agentic (browses, clicks, manages a
calendar, sends email) rather than a plain question-answering chatbot --
explicitly so a new user doesn't mentally file her as "just another AI
chat window."

**Reordered and shortened `db.ONBOARDING_STEPS`** (`db.py`): now
`["awaiting_name", "awaiting_location", "awaiting_email", "complete"]` --
location moved up to 2nd (right after name, ahead of the skippable email
question) since it's the one onboarding answer that's actually
load-bearing: timezone-correct scheduling, reminders, and the morning/
evening briefings' weather line all depend on it, so it shouldn't wait
behind an optional question a user might skip anyway. The old 4th step,
`awaiting_email_connect`, is gone entirely -- `_initial_onboarding_step`
and `save_profile_field`'s `step_for_field` map were both updated to match,
and `save_profile_field` now rejects `field='connect_email'` outright
(`ValueError`) rather than silently accepting it. Nothing about actually
connecting Gmail changed: `agents/registry.py`'s system prompt already told
Messa Gmail "can send them a connect link on request" independent of
onboarding step, so dropping the forced question doesn't remove the
capability -- it's just no longer a gate, and the user's own instruction to
hint at it is now handled by the reveal message below instead of a forced
question.

**The email step's copy was reframed, not just reworded**: it now asks for
"an email you'd want on file for creating accounts on your behalf,"
explicitly optional, explicitly telling the user that skipping it just
means Messa's own address gets used instead for that purpose -- a direct
line to the reveal message that follows.

**The reveal itself (`cli._onboarding_complete_messages`, called from
`run_message`) is deterministic, code-authored text -- not something the
model is asked to write.** This mirrors a decision this project already
made once before, for a related reason: `run_message`'s own docstring
documents an earlier bug where the system prompt asked Messa to write the
deepsearch live-view link into her own acknowledgment sentence, and a real
run trailed off mid-sentence right as the model switched into tool-call
mode, dropping the link entirely -- not a streaming bug, genuinely the
full, final text the model chose to generate that turn. The reveal message
carries the same category of must-be-correct payload (Messa's actual email
address, her actual live-view link), so it gets the same treatment: `cli.py`
detects the onboarding_step transition to `complete` (a fresh `db.get_user_by_id`
call right after `run_turn` finishes, cheap and only ever hit on the one
turn that needs it -- `UserContext.onboarding_complete`, loaded at the
START of the turn, short-circuits everything else) and sends fixed,
pre-written text rather than trusting the model to reproduce an email
address and a URL correctly, unprompted, right after finishing an unrelated
tool call. `agents/registry.py`'s `awaiting_email` prompt explicitly tells
Messa not to try writing this herself and to keep her own turn's reply
short -- the reveal is guaranteed to follow on its own.

Two short messages, not one long one (an explicit ask this phase: keep
outbound texts concise) -- the first sets expectations ("I'm not your
typical chatbot... I can browse the web, click through sites, manage your
calendar and reminders, and send emails for you"), the second is the
concrete payload: Messa's own email address, phrased as hers to manage and
safe to hand out anywhere, a line inviting the user to say the word if they
also want her managing their personal inbox (the specific hint asked for
once Gmail was dropped as a forced onboarding question), and the live-view
link, framed as where to check that inbox or watch her work. Each half
degrades independently: no `messa_email` (migration 013 not applied) or no
`live_view_share_url` (migration 006 not applied, or `LIVE_VIEW_BASE_URL`
unset) just drops that clause from the second message rather than sending
a broken one -- and if BOTH are missing, only the first message goes out,
never a silently empty second text.

## Deepsearch's acknowledgment: a standing reminder it takes a while, plus a one-time nudge that it's not just a search box

The ask: whenever Messa delegates to deepsearch, remind the user this can
take a while and that they can step away (she'll text them when it's
done), and also remind them deepsearch isn't just for simple lookups --
they can ask it to actually do things (log in, fill out forms, more
involved multi-step tasks). Both needed to land without making every
deepsearch acknowledgment noticeably longer, per the same "keep texts
concise" constraint as the onboarding reveal above.

**Both are deterministic, code-appended text in `cli.py`'s `_on_ai_message`
closure (inside `run_message`) -- the same place, and the same reasoning,
as the existing live-view link.** Rather than asking the system prompt to
remember and reword a reminder on every single delegation forever (with
all the same reliability risk documented above), the fixed reminder text
is appended right alongside the link, guaranteed to appear, guaranteed
short, and never at risk of ballooning turn over turn the way freely
regenerated model text tends to.

- **"This can take a few minutes -- go ahead and step away, I'll text you
  the second it's done."** -- appended on every deepsearch delegation's
  acknowledgment, not just the first. This is genuinely useful information
  every time (a fresh task might take a fresh few minutes), unlike the tip
  below.
- **"And it's not just browsing -- I can click through logins, fill out
  forms, and handle more involved stuff too, so feel free to ask for
  that."** -- appended only on a user's very FIRST-EVER deepsearch
  delegation, never again after that. Repeating an already-seen tip forever
  is exactly the kind of thing this project already flagged as a real
  annoyance once (see `test_link_once_per_turn.py`'s docstring: a user
  directly reported getting the same permanent live-view link twice in one
  exchange as "a little annoying") -- useful the first time someone sees
  deepsearch in action, noise on the tenth.

**New in `db.py`: `has_prior_deepsearch_session(user_id)`.** A deliberately
cheap `SELECT EXISTS (...)` against `deepsearch_sessions`, not a reuse of
the existing `list_deepsearch_sessions` (which pulls up to 20 full rows) --
this runs inline on the hot path of every deepsearch delegation, not just
when a user explicitly asks to see their session list, so it's sized for
that. Called from `_on_ai_message` BEFORE the current delegation's own
session row gets created (`build_deepsearch_subagent`'s `_run` creates it
only once the subagent actually starts, which happens after this
acknowledgment already streamed out), so it correctly reports "no prior
sessions" on that very first delegation rather than counting the
in-progress one. Same additive/no-op-safe shape as every other
`_has_table`-gated function in this file: returns `False` (not an
exception) pre-migration-004, which just means the first-time tip shows up
-- harmless, since there genuinely is no prior session on a deployment that
young either way. Resolved at most once per `run_message` call (a
`is_first_deepsearch: bool | None` cache in the closure, `None` meaning
"not checked yet") even when one incoming message triggers more than one
deepsearch delegation, so a compound ask doesn't run the query twice or
risk the tip appearing on the SECOND delegation in the same exchange just
because the first one hadn't updated anything in the DB yet.

**Deduplication within one exchange**: the existing `live_link_sent` flag
was broadened into `deepsearch_extras_sent`, now gating the link AND both
new lines together as one block -- a compound ask that fires two
deepsearch delegations in the same turn (see `test_link_once_per_turn.py`)
still only gets the link, the reminder, and the first-time tip attached to
the FIRST delegation's acknowledgment, not repeated on the second.

## Synced: `channels/resend.py`'s outbound Message-ID fix

Picked up a local fix flagged at the start of this phase: after a
successful Resend send, `send_personal_email` now overwrites its own
locally-generated `message_id` (a UUID minted before the request ever went
out, purely so the outbound `Message-ID` header had something to send)
with the `id` Resend's own response actually returns, once one comes back
-- normalized to angle-bracket form (`<...>`) if Resend didn't already
wrap it. Previously the self-generated id was what got persisted via
`db.log_outbound_personal_email`, regardless of whether Resend's own
mail-sending infrastructure preserved that exact header or substituted its
own -- meaning a reply's `In-Reply-To` could end up referencing an id that
never actually matched what shipped, silently breaking thread continuity
for anything sent through Messa's own address. Applied as-is; not
independently re-verified against a live Resend send from this sandbox
(same network restriction as everywhere else in this project).

## Verification

New `/tmp/test_onboarding_and_deepsearch_msg.py` (29 checks, all passing):
`ONBOARDING_STEPS`'/`_initial_onboarding_step`'s new order;
`save_profile_field`'s step transitions (name -> location -> email ->
complete, including a skipped email still advancing without writing the
literal string `'skip'` into `users.email`) and its rejection of the
removed `connect_email` field; `has_prior_deepsearch_session` against a
faked pool across pre-migration/brand-new-user/has-a-session states;
`_onboarding_complete_messages` (no reveal when already complete, no
reveal mid-onboarding, the full 2-message reveal on the actual transition
-- using the FRESHLY fetched name rather than the turn's stale pre-load
value, both messages' exact content checked for the email address/live
link/"just say the word" hint, and the graceful single-message degrade
when `messa_email`/`live_view_share_url` are both unavailable); and
`run_message`'s deepsearch reminder logic against a fake streaming agent
(first-ever delegation gets both the reminder and the tip, a returning
user's delegation gets the reminder but not the tip, and a compound
two-delegation turn gets the link/reminder exactly once across both sent
messages, not duplicated on the second).

Six existing `/tmp` test files from earlier phases needed updating rather
than being left to bit-rot, since `run_message` now calls two functions
(`db.get_user_by_id`, `db.has_prior_deepsearch_session`) that didn't exist
as call sites before this phase: `test_link_once_per_turn.py`,
`test_live_link_integration.py`, `test_live_link_no_token.py`,
`test_live_link_zero_preamble.py`, and `test_stall_retry.py` each needed
fakes added for those two calls (previously absent since nothing exercised
them), all re-run and confirmed still passing -- including re-confirming
the reminder text now actually shows up correctly alongside the live-view
link/no-token/zero-preamble cases those tests were written to check.
`test_onboarding_and_email_db.py` needed real updates (not just new fakes)
since two of its parts directly asserted the OLD 5-step order and the now-
removed `connect_email` field -- rewritten to assert the new 4-step order
and the field's rejection instead; its unrelated parts (the Composio
email-connection-request lifecycle: create/pending/notify/mark-connected/
expire, migrations/012) were untouched by this phase and re-confirmed
still passing as-is. `test_default_email_provider.py`, `test_empty_promise.py`,
`test_executive_quality_check.py`, and `test_personal_inbox.py` were
re-run unmodified as a broader regression check and all still pass.

**Not verified here**: a live SMS exchange against a real Sendblue number
watching the actual 2-message reveal arrive as 2 separate texts (this
sandbox has no live Sendblue account, consistent with every other such
caveat in this project); and the exact rendering/spacing of a `\n`-joined
second reveal message (the email + live-view-link line) on a real
iMessage/SMS thread -- confirmed correct as Python string content in
tests, not visually confirmed on a phone screen.
