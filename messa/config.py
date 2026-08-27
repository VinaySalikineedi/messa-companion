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
SMS_AUTO_APPROVE_DESTRUCTIVE = os.environ.get("MESSA_SMS_AUTO_APPROVE_DESTRUCTIVE", "false").strip().lower() in (
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
# Headless is required on a server with no display (HF Spaces, Phase 4) --
# default stays headed for local dev, matching the original agent2.py.
DEEPSEARCH_HEADLESS = os.environ.get("MESSA_DEEPSEARCH_HEADLESS", "false").strip().lower() in ("1", "true", "yes")
# Each user gets their own on-disk Chromium profile directory here, so
# logins/cookies survive even though the browser process itself is fully
# closed between tasks (see tools/deepsearch_tools.py).
DEEPSEARCH_PROFILES_ROOT = os.environ.get("MESSA_DEEPSEARCH_PROFILES_ROOT", "deepsearch_profiles")
# Step budget for a single deepsearch run (one delegation). Separate from
# the orchestrator's own RECURSION_LIMIT -- deep research tasks legitimately
# need many steps, and hitting this cap is expected/handled (the session is
# saved and can be resumed) rather than an error condition.
DEEPSEARCH_MAX_STEPS = int(os.environ.get("MESSA_DEEPSEARCH_MAX_STEPS", "40"))

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

    @property
    def onboarding_complete(self) -> bool:
        return self.onboarding_step == "complete"
