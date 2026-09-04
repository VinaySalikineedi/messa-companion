"""Automated verification for Phase 3: Contract Risk Auditor & Clarification Protocol.
Tests:
1. Tool availability in build_document_tools.
2. Comprehensive risk detection across all legal risk pillars.
3. Live test: Audit real service-agreement.pdf text from Gmail attachment.
4. Executive Risk Audit Report PDF compilation via _build_report_pdf.
5. Clarification Protocol & Smart Defaults prompt presence.
"""
import asyncio
import os
from pathlib import Path

from messa import cli, config
from messa.tools import document_tools as dt
from messa.tools import email_tools as et


async def run_tests():
    print("=== 1. Tool Availability & Registration ===")
    user = config.UserContext(
        user_id=1, phone_number="+12566942889", channel="sms", email_connected=True, name="Vinay"
    )
    tools = dt.build_document_tools(user)
    tool_names = [t.name for t in tools]
    assert "audit_contract" in tool_names, f"audit_contract missing from tools: {tool_names}"
    print("[PASS] audit_contract is registered in build_document_tools")

    audit_tool = next(t for t in tools if t.name == "audit_contract")

    print("\n=== 2. High-Risk Contract Detection & Redline Recommendations ===")
    dangerous_contract = """
    MASTER SERVICES AGREEMENT
    This Agreement is between Big Corp ("Client") and Small Studio ("Contractor").
    1. Services: Contractor shall build custom AI software.
    2. Compensation: Client shall pay Contractor on a Net 90 basis.
    3. Intellectual Property: Contractor assigns all right, title, and interest in all deliverables and all related inventions immediately upon creation.
    4. Indemnification: Contractor agrees to defend, indemnify, and hold harmless Client against any and all claims, losses, damages, liabilities, and reasonable attorney fees arising out of the performance of Services.
    5. Non-Compete: Contractor shall not perform software services for any entity in the technology sector for a period of two (2) years following termination.
    6. Termination: Client may terminate this Agreement at any time for convenience with zero notice.
    """
    res = await audit_tool.coroutine(
        contract_text=dangerous_contract,
        user_role="contractor",
    )
    print("Dangerous contract audit excerpt:\n" + res[:400] + "...\n")
    assert "CRITICAL RISK" in res or "HIGH RISK" in res, f"Expected critical/high risk, got: {res[:200]}"
    assert "Missing Limitation of Liability" in res, "Failed to flag missing liability cap"
    assert "One-Sided Indemnification" in res, "Failed to flag one-sided indemnity"
    assert "Premature IP Assignment" in res or "Ownership of Deliverables" in res, "Failed to flag unconditioned IP transfer"
    assert "Non-Compete" in res, "Failed to flag non-compete"
    assert "Limitation of Liability. EXCEPT FOR GROSS NEGLIGENCE" in res, "Missing standard redline clause"
    print("[PASS] Successfully detected all dangerous clauses and generated concrete redline amendments")

    print("\n=== 3. Balanced Contract Evaluation ===")
    safe_contract = """
    INDEPENDENT CONSULTING AGREEMENT
    This Agreement is entered into by and between Alpha LLC ("Client") and Beta LLC ("Consultant").
    1. Services: Consultant shall provide software advisory services.
    2. Fees: Client shall pay Consultant $10,000 monthly, payable Net 30.
    3. Limitation of Liability: Except for gross negligence, each party's aggregate liability shall be limited to total fees paid under this Agreement in the preceding 12 months. Neither party shall be liable for consequential or indirect damages.
    4. Intellectual Property: Upon Consultant's receipt of full and final payment, Consultant assigns deliverables to Client. Consultant retains all pre-existing tools and background technology.
    5. Indemnification: Each party agrees to mutually indemnify the other from third-party claims arising from gross negligence or IP infringement.
    6. Termination: Either party may terminate for convenience upon thirty (30) days' written notice, provided Client pays for all services rendered through the termination date.
    7. Governing Law: This Agreement shall be governed by the laws of the State of Delaware.
    8. Entire Agreement: This document represents the entire agreement between the parties.
    """
    safe_res = await audit_tool.coroutine(contract_text=safe_contract, user_role="consultant")
    assert "LOW RISK" in safe_res or "MODERATE RISK" in safe_res, f"Expected low/moderate risk, got: {safe_res[:200]}"
    print("[PASS] Balanced contract correctly received a low/moderate risk assessment")

    print("\n=== 4. Live Test on Real service-agreement.pdf from Gmail ===")
    user_1 = await cli.load_user_context_by_id(1)
    email_tools = et.build_email_tools(user_1)
    read_att_tool = next(t for t in email_tools if t.name == "read_email_attachment")
    real_contract_text = await read_att_tool.coroutine(message_id="1a05f0676189d07d")
    assert "Service Agreement" in real_contract_text, "Failed to fetch real contract from Gmail"

    real_audit_res = await audit_tool.coroutine(
        contract_text=real_contract_text,
        user_role="service_provider",
        generate_audit_report_pdf=True,
        report_filename="verified-service-agreement-audit",
    )
    print("Real contract audit verdict excerpt:\n" + real_audit_res[:350] + "...\n")
    assert "Missing Limitation of Liability" in real_audit_res, "Should detect missing liability cap in real contract"
    assert "Generated Report PDF at" in real_audit_res, "Should report generated audit PDF path"

    # Verify PDF was created and is non-empty
    pdf_path_line = [l for l in real_audit_res.split("\n") if "Generated Report PDF at" in l][0]
    raw_pdf_path = pdf_path_line.split("Generated Report PDF at ")[-1].strip()
    pdf_file = Path(raw_pdf_path)
    assert pdf_file.exists(), f"PDF report file does not exist: {pdf_file}"
    assert pdf_file.stat().st_size > 1000, f"PDF report is too small: {pdf_file.stat().st_size} bytes"
    print(f"[PASS] Successfully generated full executive PDF audit report ({pdf_file.stat().st_size} bytes)")

    print("\n=== 5. Clarification Protocol & Smart Defaults Verification ===")
    doc_prompt = dt.build_document_system_prompt(user_1)
    assert "CLARIFICATION PROTOCOL" in doc_prompt, "Document prompt missing clarification protocol"
    assert "Ask Once With Smart Defaults, Don't Spam" in doc_prompt, "Missing golden rule in prompt"
    assert "Net 30" in doc_prompt, "Missing Net 30 default"
    assert "Delaware" in doc_prompt, "Missing Delaware law default"

    from messa.agents.registry import _build_system_prompt
    orch_prompt = _build_system_prompt(user_1)
    assert "Clarification Protocol" in orch_prompt, "Orchestrator prompt missing clarification protocol"
    assert "audit_contract" in orch_prompt, "Orchestrator prompt missing audit_contract guidance"
    print("[PASS] Clarification Protocol and Risk Auditing rules verified across all agent prompts")

    print("\n=== ALL PHASE 3 VERIFICATIONS PASSED SUCCESSFULLY! ===")


if __name__ == "__main__":
    asyncio.run(run_tests())
