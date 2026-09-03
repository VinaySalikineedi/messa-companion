"""The new-user signup cap: lets you throttle how many BRAND NEW people can
sign up (from the moment you turn this on -- existing users are never
affected) without touching anything else about how the app works.

Off by default (config.NEW_USER_CAP == 0) -- this whole module reduces to
one cheap comparison and a guaranteed "let them through" for every existing
caller until you actually set MESSA_NEW_USER_CAP. Turned on, it's checked in
exactly one place: server.py's `_process_inbound`, right before a brand-new
phone number would otherwise get onboarded via `db.get_or_create_user`.

Three outcomes for a phone number that isn't already a `users` row:
  1. They've been explicitly admitted off the waitlist (an admin ran
     admit_from_waitlist) -- let them all the way through regardless of the
     current count.
  2. The cap isn't full yet -- let them through, same as always.
  3. The cap is full and they're not admitted -- add them to the waitlist
     (first time) or just note they're still waiting (every time after),
     and tell the caller NOT to proceed with onboarding this turn.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import config, db


@dataclass
class AdmissionDecision:
    allowed: bool
    # Only ever set when allowed=False -- the exact text to send back
    # instead of running a real agent turn. None when allowed=True (the
    # caller proceeds with its normal onboarding/turn logic).
    reply_text: str | None = None


WAITLISTED_MESSAGE = (
    "Thanks for reaching out! I'm at capacity for new users right now, so "
    "I've added you to the waitlist -- I'll text you the moment a spot "
    "opens up. Thanks for your patience!"
)
STILL_WAITING_MESSAGE = (
    "You're still on the waitlist -- I'll text you the moment a spot opens up!"
)


async def check_new_user_admission(phone_number: str) -> AdmissionDecision:
    """Call this BEFORE db.get_or_create_user for any inbound text from a
    number you haven't already resolved to an existing user this turn.
    Cheap even when the cap is off (one int comparison, no DB call at
    all)."""
    if config.NEW_USER_CAP <= 0:
        return AdmissionDecision(allowed=True)

    existing = await db.get_user_by_phone(phone_number)
    if existing is not None:
        # Already a real user -- the cap only ever governs who gets IN,
        # never anyone already here.
        return AdmissionDecision(allowed=True)

    entry = await db.get_waitlist_entry(phone_number)
    if entry is not None and entry.get("admitted_at") is not None:
        return AdmissionDecision(allowed=True)

    count = await db.count_new_users_since_baseline()
    if count < config.NEW_USER_CAP:
        return AdmissionDecision(allowed=True)

    # At/over cap, and not admitted -- waitlist them.
    if entry is None:
        await db.add_to_waitlist(phone_number)
        await db.mark_waitlist_notified(phone_number)
        return AdmissionDecision(allowed=False, reply_text=WAITLISTED_MESSAGE)
    return AdmissionDecision(allowed=False, reply_text=STILL_WAITING_MESSAGE)
