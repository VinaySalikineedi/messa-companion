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
