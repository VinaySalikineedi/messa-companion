"""Subscription plans: what each tier is allowed per day, and what it costs.

This is the one file that answers "what plans do we offer and what do they
allow" -- everything else in the usage-limits system (messa/usage.py, the
trace_tool hook in tools/common.py, the browse/email/text/connect-app
hooks) reads limits from here rather than hardcoding a number anywhere
else. Changing a limit, renaming a plan, or adding a brand-new tier is a
one-file edit; nothing downstream needs to change.

Two ways to change what's live:
  1. Edit PLANS below and redeploy -- the normal path, and the one that
     keeps a real git history of every pricing change.
  2. Set MESSA_PLANS_OVERRIDE_JSON (see _load_override below) for a fast,
     no-redeploy change -- e.g. a Hugging Face Spaces variable. Restarting
     the Space picks it up in seconds. Unset it (or delete the variable)
     to fall back to the plans defined here.

`None` means unlimited for any limit field, everywhere in this system --
in this file, in the JSON override (where it's spelled `null`), in the
database (usage_daily_counts rows are still written for cost visibility,
but nothing ever blocks), and in anything Messa or the pricing page shows
a user (rendered as the word "Unlimited", never a number or a blank).
Making a currently-capped feature unlimited on some plan someday (e.g.
"unlimited texting") is a one-line change to that plan's limit field, here
or in the override JSON -- nothing structural has to change to support it.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PlanLimits:
    """Per-day caps unless noted. None = unlimited on this plan."""

    # Outbound email sent on the user's behalf, counted as ONE feature
    # regardless of which of the three physical send paths did it: Messa's
    # own address (tools/personal_inbox_tools.py), Gmail
    # (tools/email_tools.py), or a Composio-connected third-party inbox
    # like Outlook (tools/integration_tools.py's execute_integration_tool,
    # matched by slug -- see messa/usage.py's EMAIL_SEND_ACTION_SLUGS).
    outbound_emails: int | None
    # One unit per top-level browsing session Messa opens on the user's
    # behalf (tools/deepsearch_tools.py's build_deepsearch_subagent) --
    # not per click/step inside it, and not per delegated sub-worker tab
    # that shares an already-open session.
    browse_actions: int | None
    # Every outbound SMS/iMessage Messa sends, EXCEPT the one-time-per-day
    # "you're over your limit" notice itself (see messa/usage.py) -- that
    # exemption is what keeps this limit from being able to silence its
    # own explanation.
    number_of_texts: int | None
    # NOT a daily rate -- a standing ceiling on how many apps (Gmail plus
    # any Composio-connected toolkit) can be connected AT ONCE. Enforced
    # against Composio's own live connected-accounts count at connect time,
    # not a locally cached number, so it can't drift stale.
    max_connected_apps: int | None
    # NOT a daily rate either -- a MONTHLY cap (calendar-month, the user's
    # own timezone -- see messa/usage.py's check_and_consume_monthly) on
    # total outbound-voice-call minutes, in the whole-number-of-minutes
    # sense a phone bill uses (a 90-second call bills as 2). 0 means "no
    # voice calling on this plan," not "unlimited" -- unlike every other
    # field on this dataclass, None still means unlimited here too, but 0
    # is the real value basic/pro actually carry today. Defaults to 0 when
    # absent from an older MESSA_PLANS_OVERRIDE_JSON -- see _parse_limits
    # below for why this is handled there instead of the required-fields
    # tuple.
    call_minutes: int | None = 0


@dataclass(frozen=True)
class Plan:
    id: str  # stored verbatim in users.plan_id -- never rename once live, add a new id instead
    name: str  # shown to the user (pricing page, Messa's own upgrade nudges)
    price_cents: int  # 0 for free
    limits: PlanLimits
    # Filled in once Stripe is wired up ("soon," per the original ask) --
    # None today means the pricing page/upgrade nudge falls back to a
    # placeholder contact path instead of a real checkout link. Nothing
    # else in this system needs to change when this stops being None.
    stripe_payment_link: str | None = None


# ---- The actual plan definitions ----
#
# Numbers here are the first-pass recommendation from the implementation
# proposal (see usage_limits_proposal.md), grounded in real infra costs at
# the time this was written (~$209/mo fixed platform overhead + ~$32/mo
# per active user in LLM spend). Basic's numbers are the user's own
# examples; Pro's price is the user's own number. Plus/Business names,
# prices, and limits are placeholders open to revision -- there was no
# per-plan cost data to check them against yet when this was written.
PLANS: dict[str, Plan] = {
    "basic": Plan(
        id="basic", name="Basic", price_cents=0,
        limits=PlanLimits(
            outbound_emails=10, browse_actions=5, number_of_texts=25, max_connected_apps=2,
            call_minutes=0,
        ),
    ),
    "pro": Plan(
        id="pro", name="Pro", price_cents=1200,
        limits=PlanLimits(
            outbound_emails=50, browse_actions=25, number_of_texts=150, max_connected_apps=6,
            call_minutes=0,
        ),
    ),
    "plus": Plan(
        id="plus", name="Plus", price_cents=2900,
        limits=PlanLimits(
            outbound_emails=200, browse_actions=75, number_of_texts=500, max_connected_apps=15,
            call_minutes=20,
        ),
    ),
    "business": Plan(
        id="business", name="Business", price_cents=7900,
        limits=PlanLimits(
            outbound_emails=1000, browse_actions=300, number_of_texts=2000, max_connected_apps=None,
            call_minutes=40,
        ),
    ),
}

DEFAULT_PLAN_ID = "basic"

# The ADMIN_PLAN_ID sentinel is used only internally (see messa/usage.py) to
# label an admin's own usage rows for cost visibility -- it is deliberately
# NOT a key in PLANS, so it can never appear on the pricing page or in a
# migration's plan_id validation list, matching "not put out there as
# plans but secretly." An admin account keeps a normal plan_id (whatever
# it was before) alongside users.is_admin=true; this constant is not
# stored anywhere, it exists only as a documented sentinel for readers of
# this file.
ADMIN_PLAN_ID = "__admin__"


def _parse_limits(raw: dict) -> PlanLimits:
    required = ("outbound_emails", "browse_actions", "number_of_texts", "max_connected_apps")
    missing = [k for k in required if k not in raw]
    if missing:
        raise ValueError(f"plan limits missing required field(s): {missing}")
    limits_dict = {k: raw[k] for k in required}
    # call_minutes is deliberately NOT in `required` above: an existing
    # MESSA_PLANS_OVERRIDE_JSON set in production before this field existed
    # must not suddenly raise (and brick the app on boot) the instant this
    # feature deploys. Missing means "no voice calling configured for this
    # plan yet," i.e. 0 -- the same value basic/pro carry in PLANS itself
    # -- not "unlimited."
    limits_dict["call_minutes"] = raw.get("call_minutes", 0)
    return PlanLimits(**limits_dict)


def _parse_override(raw_json: str) -> dict[str, Plan]:
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"MESSA_PLANS_OVERRIDE_JSON is not valid JSON: {e}") from e
    if not isinstance(data, dict) or not data:
        raise ValueError("MESSA_PLANS_OVERRIDE_JSON must be a non-empty JSON object of plan_id -> plan")

    parsed: dict[str, Plan] = {}
    for plan_id, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"plan {plan_id!r}: expected an object, got {type(entry).__name__}")
        for required_field in ("name", "price_cents", "limits"):
            if required_field not in entry:
                raise ValueError(f"plan {plan_id!r} is missing required field {required_field!r}")
        parsed[plan_id] = Plan(
            id=plan_id,
            name=entry["name"],
            price_cents=int(entry["price_cents"]),
            limits=_parse_limits(entry["limits"]),
            stripe_payment_link=entry.get("stripe_payment_link"),
        )
    if DEFAULT_PLAN_ID not in parsed:
        raise ValueError(
            f"MESSA_PLANS_OVERRIDE_JSON must include a {DEFAULT_PLAN_ID!r} plan (the default "
            "every new user starts on) -- rename a plan's id to it, or add one."
        )
    return parsed


def _load_plans() -> dict[str, Plan]:
    """Parsed once at import time, not per request -- see this module's own
    docstring. A malformed override fails loudly at process startup (a
    clear RuntimeError naming exactly what's wrong) rather than either
    silently falling back to defaults that no longer match what a user was
    just told on the pricing page, or surfacing as a confusing error deep
    inside some unrelated request path later."""
    raw = os.environ.get("MESSA_PLANS_OVERRIDE_JSON")
    if not raw or not raw.strip():
        return PLANS
    try:
        return _parse_override(raw)
    except ValueError as e:
        raise RuntimeError(
            f"MESSA_PLANS_OVERRIDE_JSON is set but invalid: {e}. Fix the variable's value "
            "(or unset it to fall back to the plans defined in messa/plans.py) and restart."
        ) from e


ACTIVE_PLANS: dict[str, Plan] = _load_plans()


def get_plan(plan_id: str | None) -> Plan:
    """Never raises -- an unknown/None plan_id (a user created before this
    column existed, a typo'd override) falls back to the default plan
    rather than crashing a turn. Callers that need to know "was this
    actually a valid, recognized plan_id" can check `plan_id in
    ACTIVE_PLANS` themselves (e.g. the plan-change tool, which should
    reject an unknown id outright rather than silently downgrading)."""
    return ACTIVE_PLANS.get(plan_id or DEFAULT_PLAN_ID, ACTIVE_PLANS[DEFAULT_PLAN_ID])
