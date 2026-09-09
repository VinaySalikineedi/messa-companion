"""US-only launch gate: this is a US-market-only launch, so a brand-new
phone number that isn't a US number (Sendblue delivers E.164 numbers here,
so a US number is always exactly "+1" followed by 10 digits, 12 characters
total -- no spaces, dashes, or parens) never gets onboarded. Checked in
server.py's `_process_inbound`, deliberately BEFORE the new-user-cap
waitlist gate (messa/waitlist.py) -- there's no reason to spend a waitlist
slot, or even a DB read, on a number that can never be admitted anyway.

Applies across the board to all incoming phone numbers (both new signups
and any existing accounts).

This is meant to be temporary (see the reply text below, and the "world
release" this is explicitly scoped to end at), so it stays a single flag
-- config.US_ONLY_ENABLED, defaulting to True since that's the actual
launch state being asked for here -- rather than something hardcoded, so
turning it off later is one env var (MESSA_US_ONLY_ENABLED=false), with no
code change and no redeploy of logic needed."""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import config, db

# Exactly "+1" then 10 digits -- nothing else. Deliberately strict (no
# spaces/dashes/parens tolerance): Sendblue always hands `from_number` to
# the webhook in clean E.164, so a value that doesn't match this is either
# a genuine non-US number or something malformed enough to reject either
# way.
US_NUMBER_RE = re.compile(r"^\+1\d{10}$")


@dataclass
class RegionAdmissionDecision:
    allowed: bool
    # Only ever set when allowed=False -- the exact text to send back
    # instead of proceeding with onboarding/the normal turn this message
    # would otherwise have triggered. None when allowed=True.
    reply_text: str | None = None


NON_US_MESSAGE = (
    "Sorry, I'm currently only available for phone numbers in the USA -- "
    "keep an eye out for our world release!"
)


def is_us_phone_number(phone_number: str) -> bool:
    """True only for a clean US E.164 number: "+1" followed by exactly 10
    digits. Never fuzzy -- an almost-right number (missing the country
    code, an extra/missing digit, a non-"+1" country code) is exactly what
    this gate exists to catch, not paper over."""
    return bool(US_NUMBER_RE.match((phone_number or "").strip()))


async def check_region_admission(phone_number: str) -> RegionAdmissionDecision:
    """Call this BEFORE waitlist.check_new_user_admission (and before
    db.get_or_create_user) for any inbound text -- fast regex check (no DB
    call needed). Applies to all phone numbers (both new and existing users).
    Controlled by config.US_ONLY_ENABLED so the region gate can be stopped
    or disabled at any time in the future via MESSA_US_ONLY_ENABLED=false."""
    if not config.US_ONLY_ENABLED:
        return RegionAdmissionDecision(allowed=True)
    if is_us_phone_number(phone_number):
        return RegionAdmissionDecision(allowed=True)

    return RegionAdmissionDecision(allowed=False, reply_text=NON_US_MESSAGE)
