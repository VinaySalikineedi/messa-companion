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
- Email agent's Composio integration is written against Composio's
  documented client API but **not tested against a live account** -- I
  didn't have a Composio API key while building this. Verify the action
  slugs in `messa/tools/email_tools.py` against
  https://docs.composio.dev once you add `COMPOSIO_API_KEY` and connect
  Gmail.
- No WhatsApp (per your call -- Sendblue doesn't support it; dropped for
  now, add a provider like Meta's WhatsApp Cloud API later if you want it
  back).
