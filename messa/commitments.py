"""Implicit commitment ledger (V3-autonomous.md Phase 5, section 5's
"Commitment Ledger": *"Silently tracks promises made in messages ('I'll
send the updated deck by Thursday') and nudges the user with a ready-made
draft on Wednesday afternoon."*).

A fourth instance of this codebase's isolated-preprocessing-model-call
pattern -- see messa/media_understanding.py's own docstring for the first
three (server.py's _maybe_read_inbound_pdf, _pick_contextual_reaction, and
media_understanding.py itself). Same shape: one bounded, timeout-guarded
config.build_model() call that turns a piece of text into a small
structured result, degrading to "nothing found" on ANY failure (timeout,
API error, malformed JSON, empty reply) rather than ever risking the
turn it's attached to.

Deliberately scoped to OUTBOUND EMAIL only (see config.py's own comment
above MEETING_DOSSIERS_ENABLED for the full reasoning): this module's
public entry point, maybe_record_commitment, is called fire-and-forget
(asyncio.create_task, never awaited) from tools/email_tools.py's and
tools/personal_inbox_tools.py's send_email/reply_to_email, AFTER the real
send already succeeded -- never before, never blocking it, and never
capable of turning a successful send into a failure. A message that
turns out to carry no real commitment is the overwhelmingly common case;
extract_commitment returning None for it is not an error.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date, datetime
from typing import Any

from . import config, console, db

# A commitment always needs SOME real, specific-enough due signal to be
# worth recording -- "I'll get to it eventually" is not actionable the way
# "by Thursday" or "by the 12th" is. Rather than trust the model to also
# get today's date right (it doesn't reliably know "today" from training
# data alone), the prompt below is handed today's real date explicitly and
# asked to resolve relative phrasing ("Thursday", "tomorrow", "next week")
# into a real ISO date itself, in the sender's own local timezone.
_SYSTEM_PROMPT_TEMPLATE = (
    "You read one outbound message someone sent, and decide whether it "
    "contains a concrete PROMISE the sender is making to the recipient -- "
    "something the sender said they personally will do, ideally with a "
    "deadline (\"I'll send the updated deck by Thursday\", \"I'll get you "
    "those numbers tomorrow morning\"). Today's date is {today} ({weekday}).\n\n"
    "Reply with ONLY a JSON object, no other text. If there is a real "
    "promise, reply:\n"
    '{{"commitment_summary": "<short, third-person summary of what was promised>", '
    '"due_date": "<YYYY-MM-DD, or null if no real deadline was given>", '
    '"excerpt": "<the exact sentence(s) that made the promise>"}}\n'
    "If there is no real promise -- just a question, an FYI, small talk, "
    "or a vague future intention with no real commitment (\"I'll look into "
    'it sometime") -- reply exactly: {{}}\n\n'
    "Only extract an ACTUAL commitment the sender is making to do "
    "something. Never invent a due_date that isn't actually implied by the "
    "text."
)


def _parse_model_json(text: str) -> dict[str, Any] | None:
    """Defensive JSON extraction -- models occasionally wrap the object in
    a code fence or add a stray sentence despite the prompt's "ONLY a JSON
    object" instruction. Returns None (never raises) for anything that
    doesn't parse to a dict, or parses to an empty dict (the model's own
    "no real commitment" signal)."""
    text = (text or "").strip()
    if not text:
        return None
    # Strip a ```json ... ``` or ``` ... ``` fence if present.
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    return parsed


def _parse_due_date(raw: Any) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        return datetime.strptime(raw.strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


async def extract_commitment(
    body: str, *, sender_timezone: str = config.DEFAULT_TIMEZONE,
) -> dict[str, Any] | None:
    """One isolated, bounded model call: does `body` (the text of an
    outbound email Messa just sent on the user's behalf) contain a real
    promise? Returns {"commitment_summary", "due_date" (a date or None),
    "excerpt"} or None -- None covers every failure mode (timeout, API
    error, unparseable reply) AND the normal, common "no commitment here"
    case identically, since the caller (maybe_record_commitment) treats
    both the same way: record nothing, no error surfaced anywhere."""
    body = (body or "").strip()
    if not body:
        return None
    try:
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(sender_timezone))
    except Exception:  # noqa: BLE001 - an unrecognized tz string must never break extraction
        today = datetime.now()

    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        today=today.strftime("%Y-%m-%d"), weekday=today.strftime("%A"),
    )

    try:
        model = config.build_model(
            config.COMMITMENT_EXTRACTION_MODEL_NAME,
            api_key=config.api_key_for_agent("commitment_extraction"),
        )
        result = await asyncio.wait_for(
            model.ainvoke([
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": body[:6000]},
            ]),
            timeout=config.COMMITMENT_EXTRACTION_TIMEOUT_SECONDS,
        )
        text = getattr(result, "content", None) or ""
    except Exception as e:  # noqa: BLE001 - never let a classifier failure surface anywhere
        console.system(f"[commitments] extraction call failed: {e}")
        return None

    parsed = _parse_model_json(text)
    if parsed is None:
        return None
    summary = (parsed.get("commitment_summary") or "").strip()
    if not summary:
        return None
    return {
        "commitment_summary": summary,
        "due_date": _parse_due_date(parsed.get("due_date")),
        "excerpt": (parsed.get("excerpt") or "").strip() or None,
    }


async def _guess_counterparty_name(to_address: str) -> str | None:
    """Best-effort "Jane" from "jane.smith@acme.com" -- purely cosmetic
    (used only if the model's own summary didn't already name the
    recipient), never trusted for matching -- list_open_commitments_for_
    hints matches on the real address, not this guess."""
    local = (to_address or "").split("@", 1)[0]
    local = re.sub(r"[._+\-]+", " ", local).strip()
    if not local or local.isdigit():
        return None
    return local.title()


async def _scan_and_record(
    user_id: int, to_address: str, body: str, source_type: str, sender_timezone: str,
) -> None:
    """The actual detached work -- see maybe_record_commitment below for
    why this runs as its own task rather than being awaited inline."""
    try:
        found = await extract_commitment(body, sender_timezone=sender_timezone)
        if found is None:
            return
        counterparty_name = await _guess_counterparty_name(to_address)
        await db.insert_commitment(
            user_id,
            found["commitment_summary"],
            source_excerpt=found["excerpt"],
            due_date=found["due_date"],
            counterparty_name=counterparty_name,
            counterparty_email=(to_address or "").strip().lower() or None,
            source_type=source_type,
        )
    except Exception as e:  # noqa: BLE001 - a detached task's exception must never propagate/surface
        console.system(f"[commitments] record failed for user #{user_id}: {e}")


_background_tasks: set[asyncio.Task] = set()


def maybe_record_commitment(
    user_id: int, to_address: str, body: str, *, source_type: str = "OUTBOUND_EMAIL",
    sender_timezone: str = config.DEFAULT_TIMEZONE,
) -> None:
    """Fire-and-forget entry point -- call this AFTER a send_email/
    reply_to_email tool call has already succeeded (see tools/email_tools.py
    and tools/personal_inbox_tools.py). Deliberately NOT async/awaited by
    the caller: scanning for a commitment is a nice-to-have side effect,
    never something worth adding latency to a send confirmation for (the
    exact opposite of Phase 4's own "cut latency" goal would be spending an
    extra bounded LLM call in the middle of every single outbound email).
    A no-op entirely when config.MEETING_DOSSIERS_ENABLED is off, or when
    `body`/`to_address` is empty."""
    if not config.MEETING_DOSSIERS_ENABLED:
        return
    if not (body or "").strip() or not (to_address or "").strip():
        return
    task = asyncio.create_task(
        _scan_and_record(user_id, to_address, body, source_type, sender_timezone)
    )
    # Same "task disappeared" GC-safety concern server.py's own _safe_create_task
    # helper exists for -- keep a reference until it's done so it can't be
    # garbage-collected mid-flight. _scan_and_record already swallows every
    # exception itself, so no done-callback is needed beyond this.
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
