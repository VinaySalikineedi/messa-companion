"""Post-turn "sleep & dream" consolidation
(docs/executive_agent_architecture_proposal.md section 3.C): a cheap,
fire-and-forget pass over the turn's own messages, run AFTER the reply has
already been sent -- see cli.run_message's `on_turn_complete` hook, fired
via that module's own fire-and-forget helper, and server.py's `_run_turn`,
which passes `consolidate_after_turn` in as that hook -- so this adds ZERO
latency to the user-facing response; it starts only once the reply the
user is waiting on is already on its way out.

Two things, both best-effort and completely non-fatal -- a consolidation
hiccup must never surface to the user or affect a future turn beyond "this
one link didn't get remembered":

  1. Sweep every AI/tool message produced this turn for URLs that look
     like they point at a freshly created workspace asset, and weren't
     already captured by the SYNCHRONOUS hooks in
     tools/integration_tools.py's execute_integration_tool and
     tools/document_tools.py's PDF tools (e.g. a link a subagent only
     mentioned in its own prose reply, never as a structured tool
     result) -- persisted via db.record_user_asset the same as those
     synchronous hooks. This is a SAFETY NET, not the primary mechanism;
     most assets are already captured before this pass ever runs.
  2. Bump last_referenced_at on any already-known asset (matched by exact
     URL) that was mentioned again this turn, via
     db.touch_user_asset_by_url -- keeps "most recently used" ordering
     meaningful for registry._build_system_prompt's top-N injection even
     for an asset the user just asked about again rather than a brand
     new one, and doubles as the de-dup check that stops (1) above from
     creating a second row for a URL a synchronous hook already recorded
     earlier in the very same turn.

Deliberately does NOT attempt broader "resolve contradictions" logic
(reconciling conflicting facts across turns) -- that's a genuinely
different, much larger problem (which fact is authoritative, when, for
whom) that this pass has neither the model reasoning nor the data model
to do safely yet. Scoped here to what's concretely testable and correct:
asset discovery and de-duplication.
"""
from __future__ import annotations

import re
from typing import Any

from . import config, console, db

_URL_RE = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)

# Toolkit-agnostic best-effort classification by DOMAIN, distinct from
# (and a fallback for, not a replacement of) tools/workspace_asset_tools.py's
# slug-based classify_asset_creation -- this pass only ever has a bare URL
# to go on, never the original Composio slug/arguments.
_DOMAIN_ASSET_TYPES: dict[str, str] = {
    "airtable.com": "airtable_base",
    "docs.google.com": "google_doc",
    "sheets.google.com": "google_sheet",
    "drive.google.com": "google_drive_file",
    "notion.so": "notion_page",
    "notion.site": "notion_page",
}


def _classify_url(url: str) -> str | None:
    url_lower = url.lower()
    for domain, asset_type in _DOMAIN_ASSET_TYPES.items():
        if domain in url_lower:
            return asset_type
    return None


def _message_text(msg: Any) -> str:
    """Handles both a plain-string `.content` (the common case) and the
    list-of-parts shape some LangChain message content can take (e.g. a
    multimodal message) -- same defensive-shape reasoning as this
    codebase's other message-content readers (cli.py's last_ai_text)."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(str(part.get("text") or ""))
        return " ".join(parts)
    return str(content or "")


def extract_urls(final_messages: list[Any]) -> list[str]:
    """Every distinct URL mentioned anywhere in this turn's messages, in
    first-seen order, trailing punctuation stripped (a URL at the end of
    a sentence often picks up a trailing '.'/')'/etc from the surrounding
    prose). Pure function, no I/O -- kept separate from
    consolidate_after_turn so it's trivially unit-testable without a fake
    DB."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for msg in final_messages:
        text = _message_text(msg)
        if not text:
            continue
        for raw_url in _URL_RE.findall(text):
            url = raw_url.rstrip(").,;\"'")
            if url and url not in seen_set:
                seen_set.add(url)
                seen.append(url)
    return seen


async def consolidate_after_turn(user: "config.UserContext", final_messages: list[Any]) -> None:
    """The actual post-turn hook -- see this module's own docstring for
    the full design. `user` (not just user_id) is accepted for
    forward-compatibility with a future consolidation step that needs
    more than the bare id; only `.user_id` is used today."""
    if not config.WORKSPACE_ASSETS_ENABLED:
        return
    try:
        user_id = user.user_id
    except AttributeError:
        return  # a malformed caller must never raise into a fire-and-forget task
    try:
        for url in extract_urls(final_messages):
            try:
                touched = await db.touch_user_asset_by_url(user_id, url)
                if touched:
                    continue  # already known -- just refreshed its recency, nothing more to do
                asset_type = _classify_url(url)
                if not asset_type:
                    continue  # an ordinary link (an article, a search result, ...) -- not a workspace asset
                await db.record_user_asset(
                    user_id, asset_type, title=asset_type.replace("_", " ").title(),
                    external_id=None, url=url,
                    summary="Discovered from turn output (sleep & dream pass)",
                )
            except Exception as e:  # noqa: BLE001 - one bad URL must never stop the rest
                console.system(f"[sleep-and-dream] failed to persist {url!r} (non-fatal): {e}")
    except Exception as e:  # noqa: BLE001 - background consolidation must never raise into the caller
        console.system(f"[sleep-and-dream] consolidation pass failed (non-fatal): {e}")
