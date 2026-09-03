"""Centralized usage-limit enforcement.

Every hook point in the usage-limits system -- trace_tool's feature check
(tools/common.py), execute_integration_tool's email-slug match
(tools/integration_tools.py), the deepsearch session hook
(tools/deepsearch_tools.py), and the outbound-text hooks (cli.py) -- calls
into this module instead of re-implementing plan lookup, day-bucketing, or
admin bypass itself. That's deliberate: one bug surface for "does admin
actually bypass everything" and "does None actually mean unlimited
everywhere," not five scattered if-checks that could quietly drift out of
sync with each other as more features get metered.

See messa/plans.py for what a plan/limit actually is, and
usage_limits_proposal.md for the full design this implements.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config, db, plans, timeutil

# Feature keys -- must match messa.plans.PlanLimits field names exactly for
# the three day-rate features (checked with getattr below). Defined here so
# every caller imports a constant instead of typing the string by hand.
FEATURE_OUTBOUND_EMAILS = "outbound_emails"
FEATURE_BROWSE_ACTIONS = "browse_actions"
FEATURE_NUMBER_OF_TEXTS = "number_of_texts"
FEATURE_MAX_CONNECTED_APPS = "max_connected_apps"

# Composio action slugs across email-capable toolkits that represent a real
# outbound send -- used by tools/integration_tools.py's
# execute_integration_tool to fold a 3rd-party send (Outlook, etc.) into
# the SAME outbound_emails counter as Gmail/Messa's own address. This is
# an explicit allowlist, not a substring match on "SEND" or "EMAIL" --
# Composio's own action naming isn't perfectly consistent across toolkits,
# and a fuzzy match risks both false-counting an unrelated action (one
# that merely mentions "email" in its own description) and missing a real
# one. Confirm/extend this against Composio's actual registered slug the
# first time a new email-capable toolkit gets connected (search_
# integration_tools' own output shows the real slug) -- this starting set
# wasn't verified against a live Composio catalog.
#
# GMAIL_SEND_EMAIL is deliberately NOT here: integrations_agent's own
# system prompt (integration_tools.build_integration_system_prompt) already
# tells the model email is never routed through this tool, only to
# email_agent/personal_inbox_agent -- so a Gmail send should never reach
# execute_integration_tool in the first place. Leaving it off this list
# keeps that architectural boundary explicit instead of quietly patching
# around a routing bug if one ever creeps in.
EMAIL_SEND_ACTION_SLUGS = frozenset({
    "OUTLOOK_SEND_EMAIL",
    "MICROSOFT_OUTLOOK_SEND_EMAIL",
})


def slug_is_email_send(slug: str) -> bool:
    return (slug or "").strip().upper() in EMAIL_SEND_ACTION_SLUGS


@dataclass(frozen=True)
class LimitResult:
    allowed: bool
    feature: str
    count: int
    limit: int | None
    plan_name: str

    @property
    def limit_display(self) -> str:
        return "unlimited" if self.limit is None else str(self.limit)

    @property
    def upgrade_message(self) -> str | None:
        """None when allowed (nothing for the caller to relay). Otherwise a
        string meant for the MODEL to read and relay conversationally --
        not a fixed sentence shown to the user verbatim -- matching how
        trace_tool already turns any other tool error into model-facing
        text (see tools/common.py's guarded())."""
        if self.allowed:
            return None
        return (
            f"Limit reached for {self.feature} ({self.count}/{self.limit_display}) on the "
            f"{self.plan_name} plan. Tell the user plainly that they've hit today's limit and "
            "that upgrading gets them a higher one -- a real payment page/upgrade flow is "
            "coming soon; for now just be honest about that rather than promising a link that "
            "doesn't exist yet."
        )


def _local_day(user: "config.UserContext"):
    # Falls back to UTC when the user's own timezone isn't confirmed yet --
    # same fallback UserContext.timezone_confirmed already signals
    # elsewhere (see timeutil.current_context_str), rather than trusting an
    # unconfirmed default timezone for something a limit reset depends on.
    return timeutil.local_today(user.timezone if user.timezone_confirmed else None)


async def check_and_consume(
    user: "config.UserContext", feature: str, amount: int = 1
) -> LimitResult:
    """Consumes `amount` of `feature` for `user` today and reports whether
    they're still within their plan's limit. Always records the usage
    (day-bucketed, atomically incremented) even for an admin account or an
    unlimited plan/feature -- purely so the dev team can see its own real
    OpenRouter/Browserbase spend later without digging through provider
    dashboards; `allowed` is what's forced to True for those cases, not
    the recording itself.

    Only for the three DAY-RATE features (outbound_emails, browse_actions,
    number_of_texts) -- max_connected_apps is a standing ceiling, not a
    daily rate, and goes through check_connected_apps_cap below instead."""
    plan = plans.get_plan(user.plan_id)
    limit = getattr(plan.limits, feature)
    day = _local_day(user)
    allowed, count_after = await db.check_and_increment_usage(user.user_id, feature, day, limit, amount)
    if user.is_admin:
        allowed = True
    return LimitResult(allowed=allowed, feature=feature, count=count_after, limit=limit, plan_name=plan.name)


async def peek_usage(user: "config.UserContext", feature: str) -> LimitResult:
    """Read-only version of check_and_consume -- does NOT increment. Used
    by cli.run_message's pre-agent gate: it needs to know whether the user
    is already over today's number_of_texts limit before deciding to
    invoke the agent loop at all, without that check itself counting as a
    text sent."""
    plan = plans.get_plan(user.plan_id)
    limit = getattr(plan.limits, feature)
    day = _local_day(user)
    count = await db.get_usage_count(user.user_id, feature, day)
    allowed = user.is_admin or limit is None or count <= limit
    return LimitResult(allowed=allowed, feature=feature, count=count, limit=limit, plan_name=plan.name)


async def claim_daily_notice(user: "config.UserContext", feature: str) -> bool:
    """Atomically claims the right to send today's ONE "you're over your
    limit" notice for `feature`. Reuses check_and_increment_usage's own
    atomicity rather than a second mechanism: the notice is tracked as its
    own pseudo-feature (f"limit_notice:{feature}") with limit=1, so the
    first caller today gets count_after=1 (allowed=True -- go ahead and
    send it) and every caller after that gets count_after>=2
    (allowed=False -- stay quiet). This never touches -- and never counts
    against -- the real feature's own daily counter, which is exactly what
    keeps the notice from being able to silence its own explanation."""
    day = _local_day(user)
    allowed, _ = await db.check_and_increment_usage(
        user.user_id, f"limit_notice:{feature}", day, limit=1,
    )
    return allowed


def check_connected_apps_cap(user: "config.UserContext", current_count: int) -> LimitResult:
    """max_connected_apps is a standing ceiling, not a daily rate -- there's
    nothing to day-bucket, so this is synchronous and touches no table.
    `current_count` must be the caller's own LIVE count of currently-
    connected apps (Composio's connected_accounts.list, which already
    covers Gmail too since it's connected through the same Composio user
    id -- see tools/email_tools.py and tools/integration_tools.py's own
    connect-time callers), never a cached/local number, so this can't
    block or admit a connection based on stale data."""
    plan = plans.get_plan(user.plan_id)
    limit = plan.limits.max_connected_apps
    allowed = user.is_admin or limit is None or current_count < limit
    return LimitResult(
        allowed=allowed, feature=FEATURE_MAX_CONNECTED_APPS, count=current_count,
        limit=limit, plan_name=plan.name,
    )
