"""Shared phone-number display formatting for config.SENDBLUE_NUMBER --
used by both the public landing page (messa/landing_page.py) and Messa's
own outbound email signature (messa/channels/resend.py's Messa-owned-
mailbox sends, see tools/personal_inbox_tools.py), so both places format
the same underlying number identically instead of drifting apart with two
copies of the same regex.
"""
from __future__ import annotations

import re


def format_phone_display(raw: str | None) -> str:
    """Turns config.SENDBLUE_NUMBER's raw E.164 value (e.g. "+15551234567")
    into "+1 (555) 123 4567" for a 10 or 11-digit US/Canada number -- a
    space, not a hyphen, separates the last block, matching this project's
    existing display convention (landing_page.py's own copy avoided dashes
    entirely, by request). Anything that doesn't match that shape (a
    different country code, or a format we didn't anticipate) falls back
    to the configured value exactly as-is rather than mangling it into
    something wrong -- same "don't guess, degrade honestly" instinct as
    the rest of this codebase (see e.g. weather.py's drop-if-unreliable
    contract).

    Returns "" if raw isn't configured at all (e.g. a fresh checkout of
    this repo, or a preview build without production secrets) -- callers
    decide their own fallback for that case: landing_page.py shows "our
    number"; resend.py's email signature omits the "at ..." clause
    entirely rather than showing something broken."""
    if not raw or not raw.strip():
        return ""
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    ten = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    if len(ten) == 10:
        return f"+1 ({ten[0:3]}) {ten[3:6]} {ten[6:]}"
    return raw
