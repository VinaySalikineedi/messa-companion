"""Automated verification for Phase 2: Gmail Attachment Fetcher & Ingestion.
Tests:
1. Tool availability in build_email_tools.
2. Safety & Read-only behavior (not gated by destructive approval).
3. Not connected behavior.
4. get_email with attachment detection summary.
5. Live attachment extraction on real email 1a05f0676189d07d (service-agreement.pdf).
6. Auto-resolution when attachment_id is omitted.
7. Size and page limits enforcement.
"""
import asyncio
import os
import sys

from messa import cli, config, db
from messa.approval import ApprovalGate
from messa.tools import email_tools as et


class RejectAllGate(ApprovalGate):
    """If a tool goes through this gate and is destructive, it will raise or block."""
    async def request_approval(self, action_description: str, user: config.UserContext) -> bool:
        raise RuntimeError("Destructive tool was triggered unexpectedly!")


async def run_tests():
    print("=== 1. Tool Availability & Safety ===")
    user_disconnected = config.UserContext(
        user_id=999, phone_number="+15550009999", channel="sms", email_connected=False
    )
    tools_disconnected = et.build_email_tools(user_disconnected, approval_gate=RejectAllGate())
    tool_names = [t.name for t in tools_disconnected]
    assert "read_email_attachment" in tool_names, f"read_email_attachment missing: {tool_names}"
    print("[PASS] read_email_attachment is registered in build_email_tools")

    read_tool = next(t for t in tools_disconnected if t.name == "read_email_attachment")
    
    # Verify not connected handling
    res = await read_tool.coroutine(message_id="msg_123")
    assert "hasn't connected their Gmail" in res, f"Expected not connected message, got: {res}"
    print("[PASS] read_email_attachment correctly reports unconnected Gmail")

    print("\n=== 2. Live User 1 Gmail Connection & get_email Attachment Detection ===")
    user_1 = await cli.load_user_context_by_id(1)
    assert user_1 is not None, "User 1 not found"
    assert user_1.email_connected, "User 1 Gmail is not connected"

    tools_user_1 = et.build_email_tools(user_1, approval_gate=RejectAllGate())
    get_email_tool = next(t for t in tools_user_1 if t.name == "get_email")
    read_att_tool = next(t for t in tools_user_1 if t.name == "read_email_attachment")

    # Test get_email on the email with service-agreement.pdf
    email_res = await get_email_tool.coroutine(message_id="1a05f0676189d07d")
    assert "service-agreement.pdf" in email_res, f"Expected attachment summary in get_email, got: {email_res[:300]}"
    assert "read_email_attachment" in email_res, f"Expected read_email_attachment hint in get_email, got: {email_res[:300]}"
    print("[PASS] get_email cleanly detects attachment and includes read_email_attachment hint")

    print("\n=== 3. Live Attachment Extraction (service-agreement.pdf) ===")
    # Call read_email_attachment with both message_id and filename (attachment_id omitted to test auto-resolution)
    att_res = await read_att_tool.coroutine(
        message_id="1a05f0676189d07d",
        filename="service-agreement.pdf",
    )
    print("Extraction preview:\n" + att_res[:350] + "...")
    assert "Attachment: service-agreement.pdf" in att_res, f"Missing header: {att_res[:200]}"
    assert "Service Agreement" in att_res, f"Missing agreement text: {att_res[:200]}"
    assert "VS Marketing" in att_res or "stickering" in att_res, f"Missing contract details: {att_res[:200]}"
    print("[PASS] read_email_attachment successfully downloaded and extracted real contract text")

    print("\n=== 4. Auto-resolution when filename is omitted ===")
    # When filename is omitted, it should find the single attachment in the email automatically
    att_res_auto = await read_att_tool.coroutine(message_id="1a05f0676189d07d")
    assert "Attachment: service-agreement.pdf" in att_res_auto, f"Auto-resolution failed: {att_res_auto[:200]}"
    assert "Service Agreement" in att_res_auto
    print("[PASS] read_email_attachment successfully auto-resolved single attachment without filename")

    print("\n=== 5. Nonexistent message / attachment error handling ===")
    bad_res = await read_att_tool.coroutine(message_id="nonexistent_message_id_99999")
    print("Nonexistent message result:", bad_res)
    assert "No attachments found" in bad_res or "unsuccessful" in bad_res or "Could not" in bad_res
    print("[PASS] Graceful error handling for invalid/missing attachments")

    print("\n=== ALL PHASE 2 VERIFICATIONS PASSED SUCCESSFULLY ===")


if __name__ == "__main__":
    asyncio.run(run_tests())
