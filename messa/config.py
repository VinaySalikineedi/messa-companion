"""Central configuration: env loading, model factories, shared constants.

Every other module imports settings from here instead of touching
`os.environ` directly, so switching providers/models is a one-file change.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing required environment variable: {name}. Add it to your .env file."
        )
    return val


# ---- LLM config ----
# Two separate models now, not one shared by everything:
#
# - ORCHESTRATOR_MODEL_NAME is Messa's own model -- the one that decides
#   whether/when to delegate. This is specifically where the "empty
#   promise" bug (agents/registry.py's "Critical:" paragraph, README's
#   "A delegation Messa announces but never actually makes" section) bites:
#   Messa narrating a delegation in text without actually including the
#   tool call, which the framework treats as a normal, final reply and
#   silently drops the delegation. That failure is about instruction-
#   following/tool-calling discipline specifically, which is what a
#   stronger ("pro"-tier) model buys you -- so per your call, Messa gets
#   the pro model and everyone else stays on the cheaper/faster one.
# - SUBAGENT_MODEL_NAME is used by every subagent: deepsearch (see
#   agents/registry.py's build_orchestrator, which passes it into
#   build_deepsearch_subagent) and the four dict-based subagents
#   (executive_assistant, email_agent, document_agent, routines_agent),
#   which each get `"model": subagent_model` in their spec -- deepagents'
#   own SubAgent dict supports a per-subagent `model` override (see
#   deepagents/middleware/subagents.py's SubAgent.model field); without
#   it, a subagent silently inherits whatever model create_deep_agent's
#   own `model=` argument was called with. Their jobs are narrower
#   (execute one delegated task, not decide whether to delegate at all),
#   so they're less prone to that specific failure mode, and running 5-6
#   agents per turn on the pricier model would multiply cost for little
#   extra reliability where it doesn't matter as much.
#
# The `~` prefix on the flash model string below isn't a documented
# OpenRouter convention I could find -- it's just what was already proven
# working in this project (every deepsearch run so far has gone through
# it), so ORCHESTRATOR_MODEL_NAME's default mirrors the same prefix rather
# than introducing an unverified deviation. I couldn't verify
# "~deepseek/deepseek-v4-pro" against the real OpenRouter API myself (no
# real credentials touch this sandbox, by design -- see this project's own
# testing discipline), so if it 400s on your first real message, the
# quickest fallback is dropping the tilde (`deepseek/deepseek-v4-pro`) or
# pinning the dated snapshot (`deepseek/deepseek-v4-pro-0813`) via
# MESSA_MODEL, no code change needed either way.
OPENROUTER_API_KEY = _require("OPENROUTER_API_KEY")
ORCHESTRATOR_MODEL_NAME = os.environ.get("MESSA_MODEL", "deepseek/deepseek-v4-pro-0813")
SUBAGENT_MODEL_NAME = os.environ.get("MESSA_SUBAGENT_MODEL", "~deepseek/deepseek-v4-flash-latest")

# Backward-compatible alias: kept in case anything (or you) still refers to
# "the main model" -- always Messa's own orchestrator model.
MAIN_MODEL_NAME = ORCHESTRATOR_MODEL_NAME


def build_model(model_name: str = ORCHESTRATOR_MODEL_NAME) -> ChatOpenAI:
    """Build a ChatOpenAI client pointed at OpenRouter."""
    return ChatOpenAI(
        model=model_name,
        base_url="https://openrouter.ai/api/v1",
        api_key=OPENROUTER_API_KEY,
    )


# ---- Database ----
# SQLAlchemy-style DSN in .env (postgresql+asyncpg://...). asyncpg wants a
# plain postgresql:// DSN, so we strip the driver qualifier here.
_RAW_DATABASE_URL = _require("DATABASE_URL")
DATABASE_URL = _RAW_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")

# ---- Composio (email agent) ----
COMPOSIO_API_KEY = os.environ.get("COMPOSIO_API_KEY")  # optional until Phase 2 wiring
COMPOSIO_EMAIL_ACCOUNT = os.environ.get("COMPOSIO_EMAIL_CONNECTED_ACCOUNT_ID")

# ---- Browserbase (deepsearch's remote browser) ----
# Two rounds of "Chrome isn't installed" on the HF Space -- each one a real,
# fixable bug (MCP not passing through the environment; a --browser channel
# mismatch), but both symptoms of the same underlying problem: running an
# actual Chromium inside a constrained, ephemeral container. Browserbase
# runs the browser on its own infrastructure instead; deepsearch just
# drives it over CDP (see tools/deepsearch_tools.py). Required for
# deepsearch to work at all now -- there's no local-Chromium fallback.
BROWSERBASE_API_KEY = os.environ.get("BROWSERBASE_API_KEY")

# Explicit session timeout (seconds), passed on every browserbase.create_session
# call. Root-caused a real bug: whole deepsearch sessions -- including ones
# that never touched request_human_help at all -- were cutting off at
# exactly 5 minutes. DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS (also 300s) was
# the obvious suspect but is scoped correctly (it only ever ends ONE tab's
# wait, never the whole run -- see request_human_help), and
# DEEPSEARCH_MAX_SESSION_SECONDS is 1200s, not 300s. The actual cause: we
# never passed Browserbase's own `timeout` param on session creation, so it
# silently fell back to "the Project's defaultTimeout" (confirmed via
# https://docs.browserbase.com/reference/api/create-a-session) -- a
# dashboard setting outside this code, evidently defaulting low (5 minutes)
# on this project. Passing it explicitly here removes that dependency on an
# out-of-repo dashboard setting entirely. Sized comfortably above
# DEEPSEARCH_MAX_SESSION_SECONDS (our own belt-and-suspenders cap, which
# fires first in the normal case) so this is a generous backstop, not the
# thing actually bounding a session's length day to day. Browserbase accepts
# 60-21600 seconds (max 6 hours); this is well inside that range.
BROWSERBASE_SESSION_TIMEOUT_SECONDS = int(
    os.environ.get("MESSA_BROWSERBASE_SESSION_TIMEOUT_SECONDS", "1800")
)

# ---- Live view sharing (Phase 3) ----
# Base URL for the public /live/<token> page (see server.py + db.py's
# live_share_token functions). Defaults to the now-permanent custom domain
# rather than an empty string -- an earlier version left this unset by
# default specifically so a missing/misconfigured HF secret would silently
# omit the live link rather than send a broken one, but that meant a
# genuinely-missing MESSA_LIVE_VIEW_BASE_URL secret on the Space was
# indistinguishable from "not ready yet," and cost a live-view link that
# should have gone out. Since live.textmessa.com is stable now, hardcoding
# it as the default is strictly better -- override via the env var only if
# you ever need to point this at something else (e.g. a staging Space).
LIVE_VIEW_BASE_URL = os.environ.get("MESSA_LIVE_VIEW_BASE_URL", "https://live.textmessa.com").rstrip("/")

# ---- Sendblue (SMS/iMessage channel, Phase 2) ----
# Left optional (unlike OPENROUTER_API_KEY/DATABASE_URL above) so the CLI
# keeps working with no Sendblue account at all -- messa/server.py checks
# these itself and refuses to start if they're missing, since the webhook
# server has no reason to run without them.
SENDBLUE_API_KEY = os.environ.get("SENDBLUE_API_KEY")
SENDBLUE_API_SECRET = os.environ.get("SENDBLUE_API_SECRET")
SENDBLUE_NUMBER = os.environ.get("SENDBLUE_NUMBER")  # `sendblue lines` -- your assigned number
# Optional: only checked if you've set a secret on the webhook via
# `sendblue webhooks set-receive <url>` / the dashboard. Unset = no
# verification (fine for local dev behind a private tunnel; set it once
# the URL is public).
SENDBLUE_WEBHOOK_SECRET = os.environ.get("SENDBLUE_WEBHOOK_SECRET")
# Destructive deepsearch/email actions (clicks, typing, sending) normally
# block on a human "y/n" -- there's no synchronous stdin over a text
# conversation, so the server defaults to declining them (see
# approval.DenyApprovalGate). Set this true to auto-approve instead, once
# you're comfortable with that tradeoff for your own account.
SMS_AUTO_APPROVE_DESTRUCTIVE = os.environ.get("MESSA_SMS_AUTO_APPROVE_DESTRUCTIVE", "true").strip().lower() in (
    "1", "true", "yes",
)

# ---- Identity of the local CLI user ----
# Phase 1 runs single-user over the CLI (no phone/email channel yet), so we
# pin a fixed dev identity. Phase 2 will resolve this per-channel instead
# (from the inbound SMS/email sender) rather than a constant. Name is left
# unset by default (rather than a placeholder) so a brand-new dev user goes
# through Messa's onboarding flow instead of skipping it.
DEFAULT_CLI_PHONE = os.environ.get("MESSA_CLI_USER_PHONE", "+10000000000")
DEFAULT_CLI_NAME = os.environ.get("MESSA_CLI_USER_NAME") or None
DEFAULT_TIMEZONE = os.environ.get("MESSA_DEFAULT_TIMEZONE", "America/New_York")

# ---- Runtime tuning ----
RECURSION_LIMIT = int(os.environ.get("MESSA_RECURSION_LIMIT", "100"))
SESSIONS_DIR = os.environ.get("MESSA_SESSIONS_DIR", "sessions")
OUTPUTS_DIR = os.environ.get("MESSA_OUTPUTS_DIR", "outputs")

# Deepsearch (the browsing/research subagent, formerly called
# "browser_agent"): domains it is allowed to navigate to. Empty = no restriction.
DEEPSEARCH_ALLOWED_DOMAINS: list[str] = [
    d.strip() for d in os.environ.get("MESSA_DEEPSEARCH_ALLOWED_DOMAINS", "").split(",") if d.strip()
]
# No more DEEPSEARCH_HEADLESS / DEEPSEARCH_PROFILES_ROOT -- now that the
# browser itself lives on Browserbase, not in our container, headless mode
# and profile-directory persistence are Browserbase's problem (the latter
# is now a per-user Browserbase Context instead, see db.py's
# get_browserbase_context_id). Both settings are dead now that deepsearch
# talks to a remote browser instead of launching a local one.
# Step budget for a single deepsearch run (one delegation). Separate from
# the orchestrator's own RECURSION_LIMIT -- deep research tasks legitimately
# need many steps, and hitting this cap is expected/handled (the session is
# saved and can be resumed) rather than an error condition.
DEEPSEARCH_MAX_STEPS = int(os.environ.get("MESSA_DEEPSEARCH_MAX_STEPS", "100"))

# How long (ms) @playwright/mcp waits after each action for triggered work
# (a re-render, an XHR, an animation) to "settle" before returning control --
# an unconditional per-action wait, not a timeout ceiling like the two
# below. The package's own default (500ms) is a conservative one-size-fits-
# all value; most pages settle well under that, so this is turned down a
# bit as a real, if modest, per-action latency win across every single
# click/type/navigate deepsearch makes. Not verified against a live
# Browserbase session from this sandbox (no live account here) -- if you
# start seeing "stale snapshot"/race-condition-looking failures on slower
# sites after this change, raise it back toward 500.
DEEPSEARCH_TIMEOUT_SETTLE_MS = int(os.environ.get("MESSA_DEEPSEARCH_TIMEOUT_SETTLE_MS", "200"))

# Visible cursor on the live view (see tools/deepsearch_tools.py's
# BrowserToolProvider._move_cursor_to and messa/assets/cursor_overlay.js).
# Deliberately opt-out rather than opt-in: this is a real, if small, per-
# click latency cost (one extra internal browser_evaluate round-trip plus
# the CSS transition itself, ~250ms) in exchange for a visible pointer on
# the live-view page -- an explicit trade users asked for. Turn it off
# (false) if that latency isn't worth it for your use case; the browser
# session still opens/closes exactly the same either way, only the
# cosmetic cursor-move calls are skipped.
DEEPSEARCH_CURSOR_OVERLAY = os.environ.get("MESSA_DEEPSEARCH_CURSOR_OVERLAY", "true").strip().lower() in (
    "1", "true", "yes",
)

# Whether cursor MOVEMENT is real (messa/nodehelpers/cursor_driver.mjs,
# driving genuine bezier-path mouse events via a second CDP connection --
# see tools/deepsearch_tools.py's CursorDriver) or the original DOM-only
# CSS-transition (window.__messaCursor.moveTo(), cursor_overlay.js). Only
# meaningful when DEEPSEARCH_CURSOR_OVERLAY is also on -- that flag controls
# whether a visible arrow is drawn AT ALL; this one only controls how it
# moves once there is one. An explicit, separate rollback lever on purpose:
# the real driver is a genuine extra Node subprocess talking CDP to
# Browserbase's remote browser, one more thing that can fail in production
# in a way the original DOM-only approach never could -- if it ever
# misbehaves on a real deployment, flipping this to false falls straight
# back to the original, already-proven-in-production code path without
# deleting or disabling it.
DEEPSEARCH_HUMAN_CURSOR_DRIVER = os.environ.get(
    "MESSA_DEEPSEARCH_HUMAN_CURSOR_DRIVER", "true"
).strip().lower() in ("1", "true", "yes")

# "Reading" animation on the live view (per explicit user request: deepsearch
# sitting on a static page for several seconds of LLM think-time between a
# browser_snapshot and its next action reads as "frozen," not "working"). A
# background asyncio task (see tools/deepsearch_tools.py's
# BrowserToolProvider._start_reading_animation) fires right after each
# successful browser_snapshot and scroll-centers a few real content elements
# in sequence, in a discrete "hop, pause, hop, settle" pattern rather than
# one smooth scroll -- entirely cosmetic, running in parallel with (not
# blocking) the actual agent loop, and stopped again the instant the model's
# next real tool call arrives (see _stop_reading_animation, called at the
# top of every guarded() call). Confirmed empirically in this sandbox
# (/tmp/test_mcp_concurrency.py) that @playwright/mcp pipelines concurrent
# tool calls over its stdio transport rather than serializing them, so a
# long-running background browser_evaluate call does not block/delay the
# real action's own MCP round trip. Shares messa/assets/cursor_overlay.js's
# injection (both features ride the same --init-script) but is independently
# toggleable -- turning this off still leaves the click/type cursor overlay
# on, and vice versa.
DEEPSEARCH_READING_ANIMATION = os.environ.get("MESSA_DEEPSEARCH_READING_ANIMATION", "true").strip().lower() in (
    "1", "true", "yes",
)

# Human-in-the-loop pause: deepsearch hits a login wall/CAPTCHA/2FA, calls
# request_human_help, and waits -- see tools/deepsearch_tools.py's
# request_human_help tool, db.py's deepsearch_human_help_requests functions,
# and server.py's _production_deepsearch_pause_loop for the full flow.
# Timings, in order of how the wait actually plays out:
#   1. Wait DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS for ANY activity at
#      all (60s, your number) -- nothing at all by then means give up now.
#   2. Once activity is seen, extend in
#      DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS (20s) increments for as long as
#      fresh activity keeps appearing each increment.
#   3. Regardless of how active it still looks,
#      DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS (300s = 5 minutes, your
#      number) is a hard ceiling on the total wait -- this is what actually
#      bounds the cost, not the "looks active" heuristic, since "looks
#      active" alone could in principle keep extending forever.
# DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS (5s) is how often
# request_human_help itself checks the page for change while waiting (a
# different, tighter cadence than the poll LOOP in server.py, which only
# needs to catch a new row quickly enough to send one timely SMS).
DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_INITIAL_WAIT_SECONDS", "60")
)
DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_EXTEND_SECONDS", "20")
)
DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS", "300")
)
DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS = int(
    os.environ.get("MESSA_DEEPSEARCH_HUMAN_HELP_POLL_INTERVAL_SECONDS", "5")
)

# Belt-and-suspenders hard ceiling on one deepsearch run's total wall-clock
# time (wraps inner_agent.ainvoke(...) in tools/deepsearch_tools.py's _run
# with asyncio.wait_for), independent of DEEPSEARCH_MAX_STEPS (a step COUNT,
# not a time bound -- a run stuck making slow real-world requests could
# stay under the step cap while still running for a very long time) and
# independent of the human-help timings above. This is the actual answer to
# "don't leave a rogue browser session eating up money": even a bug
# somewhere in the request_human_help wait/extend/give-up logic can't hold
# a Browserbase session open past this, because it's enforced one level
# above that feature, not only inside it. 1200s = 20 minutes -- comfortably
# above DEEPSEARCH_HUMAN_HELP_MAX_TOTAL_SECONDS (5 min) plus room for
# genuine multi-step research work around it.
DEEPSEARCH_MAX_SESSION_SECONDS = int(os.environ.get("MESSA_DEEPSEARCH_MAX_SESSION_SECONDS", "1200"))

# Multi-site delegation (delegate_website_task, tools/deepsearch_tools.py):
# how many sub-worker tabs can be running concurrently at once, across
# however many delegate_website_task calls the model makes in one turn.
# Enforced by an asyncio.Semaphore shared for the whole deepsearch run, not
# by trusting the model to count correctly -- calls beyond the cap simply
# wait for a slot. Deliberately starting LOW (your call: start at 3, raise
# later once this has run for real) rather than at the 5 you originally
# suggested -- each concurrent tab is another live browser connection (and,
# once real human-cursor lands, another Node-side cursor instance), so the
# cost of getting the cap wrong is directly a cost-and-reliability one, not
# just a performance tuning knob.
DEEPSEARCH_MAX_SUBAGENTS = int(os.environ.get("MESSA_DEEPSEARCH_MAX_SUBAGENTS", "3"))

# Step budget for ONE delegate_website_task sub-worker -- deliberately
# smaller than DEEPSEARCH_MAX_STEPS (the top-level deepsearch agent's own
# budget covering however many sites it delegates in total): a sub-worker is
# scoped to exactly ONE site and ONE goal, not the whole task, mirroring what
# deepsearch used to do sequentially per site before multi-site delegation
# existed -- not a new, more open-ended kind of task. Kept deliberately
# tight (your explicit ask: "I don't want these sessions to go longer") --
# enough for navigate -> snapshot -> a couple of actions -> a final read, not
# a multi-page exploration.
DEEPSEARCH_SUBAGENT_MAX_STEPS = int(os.environ.get("MESSA_DEEPSEARCH_SUBAGENT_MAX_STEPS", "8"))

# Step budget for executive_assistant (tasks/reminders/notes/contacts/
# calendar), deliberately small and separate from DEEPSEARCH_MAX_STEPS --
# see tools/executive_tools.py's build_executive_subagent docstring. This
# role is a handful of direct DB calls plus at most one deterministic
# quality-check retry, not an open-ended research loop, so a large step
# budget would only let a confused run wander instead of failing fast.
EXECUTIVE_RECURSION_LIMIT = int(os.environ.get("MESSA_EXECUTIVE_RECURSION_LIMIT", "14"))

# All server-side "now" comparisons (due reminders/cron) use tz-aware UTC
# datetimes explicitly (datetime.now(timezone.utc)) rather than naive
# datetime.now(), so this holds regardless of what timezone the host
# machine/container is actually set to (Neon + your deployment target both
# run in UTC).
SERVER_TIMEZONE_NOTE = "UTC"


# ---- Default morning/evening briefings ----
# Auto-provisioned for every user -- new (at account creation) and existing
# (backfilled the next time they're loaded, see db.ensure_default_briefings
# and cli.load_user_context) -- rather than something the user has to think
# to ask Messa for. Each entry becomes one cron_jobs row tagged with this
# key in the new `kind` column (migrations/011_cron_job_kind.sql), which is
# what lets ensure_default_briefings tell "already has one" apart from
# "never had one" without re-creating a briefing a user deliberately
# cancelled (existence of a row with this kind, regardless of its status,
# means "leave it alone").
#
# The prompt text below is exactly what fires through the same
# load_user_context -> build_orchestrator -> run_message path a real
# inbound text would use (see server.py's _production_cron_loop) -- there's
# no separate "briefing renderer," Messa just answers this prompt for real
# every time, using whatever's actually in the DB (and a live web_search for
# weather) at that moment.
DEFAULT_BRIEFINGS: dict[str, dict[str, str]] = {
    "morning_briefing": {
        "cron_expression": "0 7 * * *",  # 7:00 AM, the user's own local time
        "prompt_or_task": (
            "Give me my morning briefing as a short text. Check today's calendar events "
            "and any tasks due today or overdue, and list them briefly. Include a short, "
            "one-line weather summary for today for my location (use web_search for "
            "current weather -- don't guess). If I have nothing scheduled and no tasks "
            "due, don't just say so -- remind me you're happy to help set something up, "
            "or take care of anything on my mind, including things that need actually "
            "clicking around a website, since you can browse the web for me. Keep it "
            "warm, brief, and easy to scan."
        ),
    },
    "evening_briefing": {
        "cron_expression": "0 20 * * *",  # 8:00 PM, the user's own local time
        "prompt_or_task": (
            "Give me my evening briefing as a short text. Summarize what I got done "
            "today -- tasks completed and events that happened -- then give me a quick "
            "look at tomorrow: tomorrow's schedule and anything due tomorrow or still "
            "overdue. If today was quiet (nothing completed, nothing scheduled), say so "
            "briefly and remind me you're around to help plan tomorrow or take care of "
            "anything on my mind, including browsing the web for me if it'd help. Keep "
            "it warm, brief, and easy to scan."
        ),
    },
}

# For existing users who already had a simple, hand-set-up morning/evening
# briefing BEFORE this feature existed (a plain user-created cron job, via
# propose_create_recurring_cron -- `kind` is NULL on those rows, since
# tagging didn't exist yet). Per an explicit "replace entirely" product
# decision: rather than leave that old job running alongside a brand new
# tagged one (a real user would get double-texted every morning),
# db.ensure_default_briefings looks for exactly one untagged job whose own
# prompt_or_task text contains one of these keywords and retires
# (cancels) it before creating the new one. Deliberately conservative --
# see that function's docstring for why zero or multiple matches means
# "don't touch anything, just create the new one normally" rather than
# guessing.
LEGACY_BRIEFING_MATCH_KEYWORDS: dict[str, tuple[str, ...]] = {
    "morning_briefing": ("morning brief",),
    "evening_briefing": ("evening brief",),
}


@dataclass
class UserContext:
    """Resolved identity for whoever Messa is currently talking to.

    Phase 1: always the CLI user. Phase 2: built from the inbound
    channel (phone number for SMS/iMessage, address for email) via
    db.get_or_create_user().
    """

    user_id: int
    phone_number: str
    name: str | None = None
    email: str | None = None
    city: str | None = None
    timezone: str = DEFAULT_TIMEZONE
    # Whether `timezone` was actually derived from something the user told
    # us (their city/zip, via timeutil.resolve_timezone) as opposed to
    # still being the hardcoded DEFAULT_TIMEZONE placeholder. False means
    # "don't fully trust this for scheduling yet" -- see
    # timeutil.current_context_str and executive_tools.py's system prompt.
    timezone_confirmed: bool = False
    onboarding_step: str = "complete"
    channel: str = "cli"
    extra: dict = field(default_factory=dict)
    live_view_token: str | None = None

    @property
    def onboarding_complete(self) -> bool:
        return self.onboarding_step == "complete"

    @property
    def live_view_share_url(self) -> str | None:
        """The link Messa texts alongside every deepsearch delegation, or
        None if either migration 006 hasn't run yet (no token) or
        LIVE_VIEW_BASE_URL isn't configured yet -- either way, the system
        prompt treats None as "don't mention a live link this turn"."""
        if not self.live_view_token or not LIVE_VIEW_BASE_URL:
            return None
        return f"{LIVE_VIEW_BASE_URL}/live/{self.live_view_token}"
