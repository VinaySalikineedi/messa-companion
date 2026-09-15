"""Tests for Autonomous Goal-Driven Email, Approval Gate Wiring, and Draft Review."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from messa import config, db
from messa.approval import AutoApproveGate, DenyApprovalGate
from messa.agents.registry import build_orchestrator_tools
from messa.email_governor import (
    evaluate_inbound_email,
    stage_draft_as_text_file,
    EmailTrustLevel,
)

failures = []

def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


async def main():
    print("=== Testing Email Governance, Approval Gate, and Draft Staging ===")

    user = config.UserContext(
        user_id=1,
        phone_number="+12566942889",
        name="Vinay",
        is_admin=True,
    )

    # 1. Test Approval Gate Propagation
    tools_deny = {t.name: t for t in build_orchestrator_tools(user, DenyApprovalGate())}
    tools_auto = {t.name: t for t in build_orchestrator_tools(user, AutoApproveGate())}

    check("Orchestrator has send_email", "send_email" in tools_auto)
    check("Orchestrator has stage_email_draft", "stage_email_draft" in tools_auto)
    check("Orchestrator has send_draft_over_text", "send_draft_over_text" in tools_auto)

    # When DenyApprovalGate is passed, send_email is blocked by gate
    deny_res = await tools_deny["send_email"].ainvoke({
        "to": "test@example.com",
        "subject": "Hello",
        "body": "Test body",
    })
    check("DenyApprovalGate blocks send_email", "BLOCKED: user declined" in deny_res)

    # When AutoApproveGate is passed, send_email proceeds past approval gate
    # (it may fail at network/Resend layer if API key isn't active, but does NOT fail with 'BLOCKED: user declined')
    auto_res = await tools_auto["send_email"].ainvoke({
        "to": "test@example.com",
        "subject": "Hello",
        "body": "Test body",
    })
    check("AutoApproveGate does NOT block with user declined", "BLOCKED: user declined" not in auto_res)

    # 2. Test Draft .txt File Staging
    draft_res = await stage_draft_as_text_file(
        user_id=user.user_id,
        to="legalbeef.media@gmail.com",
        subject="Collaboration with Messa",
        body="Hi Loren, love your Legal Beef content on Instagram!",
    )
    check("Draft file exists on disk", Path(draft_res["file_path"]).is_file())
    check("Draft filename is .txt", draft_res["filename"].endswith(".txt"))
    check("Draft media_url generated", draft_res["media_url"] is not None and "/files/" in draft_res["media_url"])

    # Verify file contents
    content = Path(draft_res["file_path"]).read_text()
    check("Draft file contains To header", "TO: legalbeef.media@gmail.com" in content)
    check("Draft file contains Subject header", "SUBJECT: Collaboration with Messa" in content)
    check("Draft file contains Body", "Legal Beef" in content)

    # 3. Test stage_email_draft tool and task scratchpad persistence
    stage_tool_res = await tools_auto["stage_email_draft"].ainvoke({
        "to": "loren@legalbeef.com",
        "subject": "Quick Intro",
        "body": "Hi Loren, Vinay wanted me to connect with you.",
    })
    check("stage_email_draft reports staged", "Draft staged successfully" in stage_tool_res)

    task = await db.get_active_task(user.user_id)
    check("Active task exists", task is not None)
    artifacts = (task or {}).get("artifacts", {})
    check("Task scratchpad contains pending_email_draft", "pending_email_draft" in artifacts)
    staged = artifacts.get("pending_email_draft", {})
    check("Staged draft has correct to address", staged.get("to") == "loren@legalbeef.com")
    check("Staged draft has media_url", bool(staged.get("media_url")))

    # 4. Test send_draft_over_text
    send_draft_res = await tools_auto["send_draft_over_text"].ainvoke({})
    check("send_draft_over_text succeeds or returns draft text", "Sent draft" in send_draft_res or "Subject: Quick Intro" in send_draft_res)

    # 5. Test evaluate_inbound_email with Project Capsule goal linkage
    eval_res = await evaluate_inbound_email(
        user_id=user.user_id,
        from_address="partner@crossriverbank.com",
        subject="Bank Partnership Discussion",
        body_text="We received your proposal regarding Messa card sponsorship.",
    )
    # User #1 has Project Capsule #1 'Messa — Bank Partner Outreach' containing Cross River Bank
    check("Inbound email links to Project Capsule #1", eval_res.get("project_capsule_id") == 1 or "Bank" in str(eval_res.get("project_title")))
    check("Sender recognized as authorized partner", eval_res.get("trust_level") == EmailTrustLevel.AUTHORIZED_PARTNER.value)
    check("Active goal populated", bool(eval_res.get("goal")))

    # 6. Test evaluate_inbound_email for unverified sender
    eval_unverified = await evaluate_inbound_email(
        user_id=user.user_id,
        from_address="random_stranger_12345@unknown.xyz",
        subject="Can we meet?",
        body_text="I want to schedule 30 mins with Vinay.",
    )
    check("Unverified sender requires permission", eval_unverified.get("trust_level") == EmailTrustLevel.NEEDS_PERMISSION.value)
    check("Unverified sender is not authorized", eval_unverified.get("is_authorized") is False)

    # 7. Test Revocation of Sender Permissions
    check("Orchestrator has revoke_email_sender_access", "revoke_email_sender_access" in tools_auto)
    check("Orchestrator has authorize_email_sender_access", "authorize_email_sender_access" in tools_auto)
    check("Orchestrator has list_email_sender_permissions", "list_email_sender_permissions" in tools_auto)

    # Revoke partner@crossriverbank.com
    rev_res = await tools_auto["revoke_email_sender_access"].ainvoke({"sender_or_domain": "partner@crossriverbank.com"})
    check("revoke_email_sender_access executes", "Revoked automated access" in rev_res)

    # Re-evaluate inbound email: must be REVOKED now, overriding the Project Capsule!
    eval_revoked = await evaluate_inbound_email(
        user_id=user.user_id,
        from_address="partner@crossriverbank.com",
        subject="Bank Partnership Discussion",
        body_text="We received your proposal regarding Messa card sponsorship.",
    )
    check("Revoked sender overrides capsule goal", eval_revoked.get("trust_level") == EmailTrustLevel.REVOKED.value)
    check("Revoked sender is not authorized", eval_revoked.get("is_authorized") is False)
    check("Revoked sender is_revoked is True", eval_revoked.get("is_revoked") is True)

    # List permissions
    list_res = await tools_auto["list_email_sender_permissions"].ainvoke({})
    check("list_email_sender_permissions includes revoked sender", "partner@crossriverbank.com" in list_res)

    # Restore authorization
    auth_res = await tools_auto["authorize_email_sender_access"].ainvoke({"sender_or_domain": "partner@crossriverbank.com"})
    check("authorize_email_sender_access executes", "Authorized automated access" in auth_res)

    eval_restored = await evaluate_inbound_email(
        user_id=user.user_id,
        from_address="partner@crossriverbank.com",
        subject="Bank Partnership Discussion",
        body_text="We received your proposal regarding Messa card sponsorship.",
    )
    check("Restored sender is authorized partner again", eval_restored.get("trust_level") == EmailTrustLevel.AUTHORIZED_PARTNER.value)
    check("Restored sender is_authorized is True", eval_restored.get("is_authorized") is True)

    print(f"\nCompleted with {len(failures)} failures.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
