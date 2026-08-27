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
# Main reasoning model used by the orchestrator and all subagents unless
# a subagent overrides it. Swap the model string to change providers/cost.
OPENROUTER_API_KEY = _require("OPENROUTER_API_KEY")
MAIN_MODEL_NAME = os.environ.get("MESSA_MODEL", "~deepseek/deepseek-v4-flash-latest")
CRITIC_MODEL_NAME = os.environ.get("MESSA_CRITIC_MODEL", MAIN_MODEL_NAME)


def build_model(model_name: str = MAIN_MODEL_NAME) -> ChatOpenAI:
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

# All server-side "now" comparisons (due reminders/cron) use tz-aware UTC
# datetimes explicitly (datetime.now(timezone.utc)) rather than naive
# datetime.now(), so this holds regardless of what timezone the host
# machine/container is actually set to (Neon + your deployment target both
# run in UTC).
SERVER_TIMEZONE_NOTE = "UTC"


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
