"""Deterministic (zero-LLM) inbound-email triage -- V3-autonomous.md
Phase 3, "Three-Tier Inbound Email Triage": VIP -> instant SMS,
Updates/Receipts -> rolled into the next 7am morning briefing,
Marketing -> rolled into the next Sunday evening briefing.

Why deterministic, not an isolated LLM call (the pattern config.py's own
media_understanding.py comment describes): the explicit ask for this phase
was "don't bloat Messa or spend tokens classifying mail she never even
mentions" -- a regex/keyword classifier costs nothing, runs in microseconds,
and (unlike an LLM call, isolated or not) can never drift, hallucinate, or
somehow leak into Messa's own system prompt. The tradeoff is real: this
classifier is a heuristic, not a judgment call, and will misclassify some
mail. Two design choices bound that risk: (1) VIP is the DEFAULT for
anything that doesn't clearly look automated -- today's exact behavior is
preserved for every email this classifier isn't confident about, so a real
person's message can never silently get buried in a digest; (2) among
automated-looking mail, ambiguous cases resolve to the 'daily' tier (next
morning briefing, at most same-day delay) rather than 'weekly' (up to six
days) -- the same "pick the safe direction to be wrong in" rule this
codebase already applies elsewhere (db._ROUTINE_EMAIL_RE, db.is_muted_sender).
A user can always correct a specific sender with mark_email_vip
(registry.py) if the heuristic gets it wrong.

Isolation: mirrors briefings.py's and weather.py's own convention -- zero
dependency on server.py or agents/registry.py, so this is unit-testable
standalone and can never accidentally end up importing (or being imported
by) anything that runs a model turn.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from email.utils import parseaddr

from . import config, db

TIER_VIP = "vip"
TIER_DAILY = "daily"    # rolled into the next morning_briefing
TIER_WEEKLY = "weekly"  # rolled into the next Sunday-evening evening_briefing

# A sender local-part shaped like an automated mailbox -- not proof of
# anything on its own (both a receipt and a marketing blast commonly come
# from "no-reply@..."), just the first signal that this probably isn't a
# real person typing to another real person. Requires the pattern at the
# START of the local part (a real human's address containing "order" or
# "update" somewhere isn't unusual; one that STARTS with it almost always
# is an automated mailbox).
_AUTOMATED_LOCALPART_RE = re.compile(
    r"^(no.?reply|do.?not.?reply|notifications?|alerts?|receipts?|orders?|"
    r"billing|statements?|confirmations?|updates?|shipping|delivery|"
    r"newsletters?|marketing|promos?|promotions?|deals|offers|digest|"
    r"support|automated|system|mailer|bounce)[.\-_+0-9]*(?=@|$)",
    re.IGNORECASE,
)

# Marketing/promotional signals -- checked against the subject plus a
# capped slice of the body (a footer's "unsubscribe" line is enough on its
# own; there's no need to scan an entire long email to find it).
_MARKETING_RE = re.compile(
    r"unsubscribe|opt.?out|view (this|it) in (your )?browser|"
    r"manage (your )?(email )?preferences|update (your )?(email )?preferences|"
    r"\d+%\s*off|limited time|shop now|promo ?code|flash sale|deal of the day|"
    r"free shipping|exclusive offer|% off",
    re.IGNORECASE,
)

# Receipt/transactional/update signals -- checked mainly against the
# subject (the most reliable place a sender states what an automated
# message actually is).
_RECEIPT_UPDATE_RE = re.compile(
    r"receipt|invoice|order (confirmation|confirmed|placed|shipped|delivered)|"
    r"has shipped|out for delivery|\bdelivered\b|payment (received|confirmation|failed)|"
    r"\bstatement\b|subscription (renewed|renewal|receipt)|"
    r"appointment (confirmation|reminder)|reservation (confirmation|reminder)|"
    r"account (statement|activity)|password (reset|changed)|security alert|"
    r"sign.?in alert|verification code|booking confirm",
    re.IGNORECASE,
)


def _clean_address(from_address: str) -> str:
    parsed = parseaddr(from_address or "")[1].strip().lower()
    return parsed or (from_address or "").strip().lower()


def _local_part(from_address: str) -> str:
    addr = _clean_address(from_address)
    return (addr.split("@", 1)[0] if "@" in addr else addr).strip()


async def classify_inbound_email(user_id: int, from_address: str, subject: str, body_text: str) -> str:
    """Returns TIER_VIP / TIER_DAILY / TIER_WEEKLY for one inbound personal
    email. Order of checks:

    1. db.is_vip_sender override -- the user has explicitly said this
       sender should always be instant, regardless of what it looks like.
    2. Receipt/update signal in the subject -- highest-confidence
       "this is a transactional email" check, so it wins even if a
       marketing-shaped phrase also happens to appear in the body.
    3. Marketing signal anywhere in subject+body.
    4. Automated-looking local part with no specific marketing/receipt
       match -- ambiguous, resolves to 'daily' (the safer, sooner tier).
    5. Otherwise: TIER_VIP -- doesn't look automated at all, treated as a
       real person (today's exact behavior)."""
    if config.USER_LISTS_ENABLED and await db.is_vip_sender(user_id, from_address):
        return TIER_VIP

    subject = subject or ""
    body_text = body_text or ""
    haystack = f"{subject}\n{body_text[:2000]}"

    if _RECEIPT_UPDATE_RE.search(subject):
        return TIER_DAILY
    if _MARKETING_RE.search(haystack):
        return TIER_WEEKLY
    if _AUTOMATED_LOCALPART_RE.match(_local_part(from_address)):
        return TIER_DAILY
    return TIER_VIP


def summarize_for_digest(from_address: str, subject: str) -> str:
    """One short line for a queued item -- shown a sender's domain (what a
    person actually recognizes at a glance, e.g. "amazon.com") rather than
    the full mailbox address, plus the subject."""
    addr = _clean_address(from_address)
    domain = addr.rsplit("@", 1)[1].strip().lower() if "@" in addr else addr.strip()
    label = domain or addr or "unknown sender"
    clean_subject = (subject or "(no subject)").strip() or "(no subject)"
    return f"{label} -- {clean_subject}"


def tiers_for_briefing_kind(kind: str, local_now: datetime) -> list[str]:
    """Which email_digest_queue tiers should be rolled into a given
    briefing kind's send, right now. morning_briefing always carries the
    'daily' tier. evening_briefing carries the 'weekly' tier only on a
    LOCAL Sunday (weekday() == 6) -- reusing the daily 8pm evening_briefing
    cron this codebase already runs as the weekly digest's delivery slot,
    rather than provisioning a brand new cron kind/background loop just for
    a once-a-week send. Single source of truth: called from both
    briefings.py (to decide what to render) and server.py's
    _send_one_briefing (to decide what to clear after a successful send),
    so the two can never drift apart."""
    if kind == "morning_briefing":
        return [TIER_DAILY]
    if kind == "evening_briefing" and local_now.weekday() == 6:
        return [TIER_WEEKLY]
    return []


async def render_digest_section(user_id: int, tiers: list[str]) -> str | None:
    """Renders one plain-text line/section for whichever tiers are due
    right now, or None if there's nothing queued -- never raises, same
    "degrade gracefully, never blank the whole briefing" convention as the
    rest of briefings.py."""
    if not tiers or not config.EMAIL_TRIAGE_ENABLED:
        return None
    items: list[dict[str, Any]] = []
    for tier in tiers:
        items.extend(await db.get_pending_email_digest_items(user_id, tier))
    if not items:
        return None
    label = "Emails" if TIER_WEEKLY not in tiers else "This week's marketing/promo emails"
    lines = [it["summary"] for it in items]
    if len(lines) == 1:
        return f"{label}: {lines[0]}."
    return f"{label} ({len(lines)}): " + "; ".join(lines) + "."


async def clear_flushed_digest_tiers(job: dict[str, Any]) -> None:
    """Called after a briefing actually SENDS successfully (never before,
    and never on a failed send -- a Sendblue failure leaves queued items in
    place so they're retried in the next due briefing instead of silently
    lost, same reliability posture as digest_queue's own clear-after-send
    in server.py's _flush_one)."""
    tz_name = job.get("user_timezone") or config.DEFAULT_TIMEZONE
    try:
        local_now = datetime.now(ZoneInfo(tz_name))
    except Exception:
        local_now = datetime.now(timezone.utc)
    for tier in tiers_for_briefing_kind(job.get("kind") or "", local_now):
        await db.clear_email_digest_items(job["user_id"], tier)
