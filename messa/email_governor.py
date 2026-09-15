"""Email Governor: Autonomous Goal-Driven Email & Draft Delivery.

Transforms Messa's personal inbox into an autonomous executive assistant:
1. Evaluates incoming emails against active Project Capsules and goals.
2. Enforces sender authorization (anti-injection & boss permission checks).
3. Classifies stakes (low-stakes autonomous vs. high-stakes confirmation).
4. Formats email drafts into clean .txt files served via secure document tokens
   for zero-bloat SMS/iMessage review.
"""

from __future__ import annotations

import os
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any

from . import config, console, db


class EmailTrustLevel(str, Enum):
    REVOKED = "revoked"                        # Boss explicitly revoked auto-privileges
    AUTHORIZED_PARTNER = "authorized_partner"  # Pre-approved by boss or matched in a project capsule
    NEEDS_PERMISSION = "needs_permission"      # Unverified external sender asking for time or resources
    AUTOMATED_DIGEST = "automated_digest"      # Newsletters, system alerts, no human


class EmailStakeLevel(str, Enum):
    HIGH_STAKES = "high_stakes"  # Legal, financial, commitments, cold pitches
    LOW_STAKES = "low_stakes"    # Scheduling within approved windows, factual info, confirmations


def clean_address(addr: str) -> str:
    """Normalize email address to lowercase bare address."""
    match = re.search(r'[\w\.-]+@[\w\.-]+', addr or "")
    return match.group(0).lower() if match else (addr or "").strip().lower()


async def evaluate_inbound_email(
    user_id: int,
    from_address: str,
    subject: str,
    body_text: str,
) -> dict[str, Any]:
    """Evaluate an inbound email to determine:
    1. Associated Project Capsule and Goal (if any)
    2. Sender authorization status
    3. Suggested autonomous action vs. boss permission check
    """
    clean_from = clean_address(from_address)
    from_domain = clean_from.split("@")[-1] if "@" in clean_from else ""

    # 0. Check if sender or domain is explicitly revoked
    is_revoked = False
    try:
        revoked_rows = await db.get_user_list_items(user_id, "revoked_email_senders")
        for r in revoked_rows:
            val = (r.get("item_value") or "").lower().strip()
            if val and (val == clean_from or (from_domain and val == from_domain)):
                is_revoked = True
                break
    except Exception as e:
        console.system(f"[email_governor] Revoked sender lookup error: {e}")

    if is_revoked:
        return {
            "from_address": clean_from,
            "from_domain": from_domain,
            "trust_level": EmailTrustLevel.REVOKED.value,
            "is_authorized": False,
            "is_revoked": True,
            "project_capsule_id": None,
            "project_title": None,
            "goal": "",
        }

    # 1. Search active Project Capsules for goal alignment
    matched_capsule = None
    capsule_goal = ""
    try:
        pool = await db.get_pool()
        async with pool.acquire() as conn:
            if await db._has_table(conn, "project_capsules"):
                rows = await conn.fetch(
                    """
                    SELECT p.*, c.prompt_or_task, c.status AS cron_status
                    FROM project_capsules p
                    LEFT JOIN cron_jobs c ON c.id = p.cron_job_id
                    WHERE p.user_id = $1 AND p.status = 'active'
                    ORDER BY p.created_at DESC
                    """,
                    user_id,
                )
                clean_kw = from_domain.replace(".com", "").replace(".org", "").replace(".net", "") if from_domain else ""
                clean_kw_norm = re.sub(r"[^a-z0-9]", "", clean_kw)
                for c in rows:
                    goal_text = str(c.get("goal") or "").lower()
                    title_text = str(c.get("title") or "").lower()
                    prompt_text = str(c.get("prompt_or_task") or "").lower()

                    norm_goal = re.sub(r"[^a-z0-9]", "", goal_text)
                    norm_title = re.sub(r"[^a-z0-9]", "", title_text)

                    target_match = (
                        (clean_from and clean_from in goal_text)
                        or (from_domain and from_domain in goal_text)
                        or (clean_kw and clean_kw in goal_text)
                        or (clean_kw_norm and len(clean_kw_norm) >= 4 and clean_kw_norm in norm_goal)
                        or (clean_from and clean_from in title_text)
                        or (clean_kw and clean_kw in title_text)
                        or (clean_kw_norm and len(clean_kw_norm) >= 4 and clean_kw_norm in norm_title)
                        or (clean_from and clean_from in prompt_text)
                    )
                    if target_match:
                        matched_capsule = dict(c)
                        capsule_goal = c.get("goal") or c.get("prompt_or_task") or c.get("title") or ""
                        break
    except Exception as e:
        console.system(f"[email_governor] Project capsule lookup error: {e}")

    # 2. Check if sender is on pre-approved list
    is_authorized = False
    if matched_capsule:
        is_authorized = True
    else:
        try:
            approved_rows = await db.get_user_list_items(user_id, "authorized_email_senders")
            for r in approved_rows:
                val = (r.get("item_value") or "").lower().strip()
                if val and (val == clean_from or (from_domain and val == from_domain)):
                    is_authorized = True
                    break
        except Exception as e:
            console.system(f"[email_governor] Authorized sender lookup error: {e}")

    # 3. Classify Trust Level
    if is_authorized:
        trust_level = EmailTrustLevel.AUTHORIZED_PARTNER
    else:
        trust_level = EmailTrustLevel.NEEDS_PERMISSION

    return {
        "from_address": clean_from,
        "from_domain": from_domain,
        "trust_level": trust_level.value,
        "is_authorized": is_authorized,
        "is_revoked": False,
        "project_capsule_id": matched_capsule["id"] if matched_capsule else None,
        "project_title": matched_capsule["title"] if matched_capsule else None,
        "goal": capsule_goal,
    }


async def revoke_sender_access(
    user_id: int,
    sender_or_domain: str,
    reason: str = "",
) -> dict[str, Any]:
    """Revoke a sender or domain's automated access (scheduling, replies)."""
    cleaned = (sender_or_domain or "").strip().lower()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    clean_addr = clean_address(cleaned) if "@" in cleaned else cleaned

    await db.remove_user_list_item(user_id, "authorized_email_senders", clean_addr)
    await db.add_user_list_item(user_id, "revoked_email_senders", clean_addr)
    return {
        "sender": clean_addr,
        "status": "revoked",
        "reason": reason,
    }


async def authorize_sender_access(
    user_id: int,
    sender_or_domain: str,
) -> dict[str, Any]:
    """Grant or restore automated coordination access to a sender or domain."""
    cleaned = (sender_or_domain or "").strip().lower()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    clean_addr = clean_address(cleaned) if "@" in cleaned else cleaned

    await db.remove_user_list_item(user_id, "revoked_email_senders", clean_addr)
    await db.add_user_list_item(user_id, "authorized_email_senders", clean_addr)
    return {
        "sender": clean_addr,
        "status": "authorized",
    }


async def list_sender_access_rules(user_id: int) -> dict[str, list[str]]:
    """Return all currently authorized and revoked senders/domains."""
    auth_rows = await db.get_user_list_items(user_id, "authorized_email_senders")
    rev_rows = await db.get_user_list_items(user_id, "revoked_email_senders")
    return {
        "authorized": [r["item_value"] for r in auth_rows if r.get("item_value")],
        "revoked": [r["item_value"] for r in rev_rows if r.get("item_value")],
    }


async def stage_draft_as_text_file(
    user_id: int,
    to: str,
    subject: str,
    body: str,
    prefix: str = "draft",
) -> dict[str, Any]:
    """Write an email draft to a formatted .txt file in outputs/ and create a document share token.
    Enables instant QuickLook preview on iOS/Android via MMS without text blob truncation.
    """
    outputs_dir = Path(config.OUTPUTS_DIR).resolve()
    outputs_dir.mkdir(parents=True, exist_ok=True)

    clean_target = re.sub(r'[^a-zA-Z0-9]', '_', clean_address(to))[:20]
    filename = f"{prefix}_{clean_target}_{int(time.time())}.txt"
    file_path = outputs_dir / filename

    content = (
        f"TO: {to}\n"
        f"SUBJECT: {subject}\n"
        f"--------------------------------------------------\n\n"
        f"{body.strip()}\n"
    )

    file_path.write_text(content, encoding="utf-8")

    token = await db.create_document_share(
        user_id=user_id,
        file_path=str(file_path),
        filename=filename,
        media_type="text/plain",
    )

    media_url = f"{config.LIVE_VIEW_BASE_URL}/files/{token}" if token else None

    return {
        "file_path": str(file_path),
        "filename": filename,
        "token": token,
        "media_url": media_url,
    }
