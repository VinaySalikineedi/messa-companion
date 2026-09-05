"""Rigorous test suite for Messa's executive assistant email persona,
prompt engineering, edge-case sanitization, and end-to-end tool execution.
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
load_dotenv(REPO_ROOT / ".env")
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import config  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import personal_inbox_tools  # noqa: E402

failures = []


def check(label: str, cond: bool, extra: str = ""):
    status = "PASS" if cond else "FAIL"
    detail = f" -- {extra}" if (not cond and extra) else ""
    print(f"[{status}] {label}{detail}")
    if not cond:
        failures.append(label)


# ============================================================================
# 1. Sanitizer Edge Cases
# ============================================================================
def test_sanitizer_edge_cases():
    print("\n--- 1. Sanitizer Edge Cases ---")

    cases = [
        # (body, user_name, expected_substring, should_not_contain, description)
        (
            "Looking forward to it!\n\nBest,\nVinay",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "Standard 'Best,\\nVinay'",
        ),
        (
            "Looking forward to it!\n\nBest regards,\nVinay",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "Standard 'Best regards,\\nVinay'",
        ),
        (
            "See you then.\n\nWarmly,\nVinay",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "'Warmly,\\nVinay'",
        ),
        (
            "Thanks for the update.\n\nCheers,\nVinay",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "'Cheers,\\nVinay'",
        ),
        (
            "Understood.\n\nSincerely,\nVinay",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "'Sincerely,\\nVinay'",
        ),
        (
            "Sounds good.\n\nThanks,\nVinay Salikineedi",
            "Vinay Salikineedi",
            "Messa (Assistant to Vinay Salikineedi)",
            None,
            "Full multi-word name 'Vinay Salikineedi'",
        ),
        (
            "Here is the contract.\n\n- Mary-Jane",
            "Mary-Jane",
            "Messa (Assistant to Mary-Jane)",
            None,
            "Hyphenated name '- Mary-Jane'",
        ),
        (
            "Let's do it.\n\n\nBest,\n\nVinay\n\n",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "Excessive newlines and trailing whitespace",
        ),
        (
            "Case test.\n\nbest,\nvinay",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "Lower-case 'best,\\nvinay'",
        ),
        (
            "Uppercase test.\n\nBEST REGARDS,\nVINAY",
            "Vinay",
            "Messa (Assistant to Vinay)",
            None,
            "Upper-case 'BEST REGARDS,\\nVINAY'",
        ),
        # Negative cases (must NOT be falsely replaced)
        (
            "I spoke with Vinay earlier and he agreed to the terms.",
            "Vinay",
            "agreed to the terms.",
            "Messa (Assistant to Vinay)",
            "User name mentioned in middle of sentence is NOT touched",
        ),
        (
            "Vinay said that noon works great. Let me know!\n\nBest,\nMessa",
            "Vinay",
            "Best,\nMessa",
            "Assistant to Vinay",
            "Already signed by Messa is left untouched",
        ),
        (
            "We need a grand plan for this project.",
            "Dan",
            "grand plan",
            "Messa (Assistant to Dan)",
            "Name substring inside another word ('Dan' in 'grand') is NOT matched",
        ),
        (
            "",
            "Vinay",
            "",
            "Messa",
            "Empty string body handled safely without crash",
        ),
        (
            "Hello there!",
            None,
            "Hello there!",
            "Messa",
            "None user_name handled safely without crash",
        ),
        (
            "Special regex chars.\n\nBest,\nDr. Alex (MD)",
            "Dr. Alex (MD)",
            "Messa (Assistant to Dr. Alex (MD))",
            None,
            "User name with special regex characters '(MD)' and '.'",
        ),
    ]

    for body, name, expected_sub, forbidden_sub, desc in cases:
        res = personal_inbox_tools._sanitize_assistant_email_body(body, name)
        pass_expected = (expected_sub in res) if expected_sub else (res == body)
        pass_forbidden = (forbidden_sub not in res) if forbidden_sub else True
        check(f"Sanitizer: {desc}", pass_expected and pass_forbidden, f"Result was: {repr(res)}")


# ============================================================================
# 2. System Prompts under Diverse User Profiles
# ============================================================================
def test_system_prompt_profiles():
    print("\n--- 2. System Prompt Profiles ---")

    profiles = [
        ("Vinay", "messa"),
        ("Vinay Salikineedi", "gmail"),
        ("Mary-Jane O'Connor", "messa"),
        ("A", "messa"),
        (None, "messa"),
    ]

    for name, provider in profiles:
        user = config.UserContext(
            user_id=99,
            phone_number="+15555555555",
            name=name,
            timezone="America/Chicago",
            messa_email_local_part="testuser",
            default_email_provider=provider,
        )

        inbox_prompt = personal_inbox_tools.build_personal_inbox_system_prompt(user)
        orch_prompt = registry._build_system_prompt(user)

        target_name = name or "the user"

        # Post-compression (plans/glowing-forging-pumpkin.md Part 3): same
        # rules (third person, no first-person impersonation, no name
        # sign-off), now stated in one short "Voice in the body:" paragraph
        # rather than the old separate bulleted lines -- see personal_inbox_
        # tools.py's build_personal_inbox_system_prompt.
        check(f"Inbox prompt carries assistant persona for name={name!r}",
              f"Voice in the body: you're Messa, {target_name}'s assistant, writing on "
              "their behalf" in inbox_prompt)
        check(f"Inbox prompt carries third-person rules for name={name!r}",
              f"{target_name} asked me to follow up" in inbox_prompt)
        check(f"Inbox prompt carries prohibition on first person for name={name!r}",
              f"never first-person as {target_name}" in inbox_prompt)

        # Check orchestrator prompt -- this now lives in the shared email/
        # calendar/tasks "Routing --" paragraph's email bullet.
        check(f"Orchestrator carries personal_inbox assistant instruction for name={name!r}",
              "always write third-person on their behalf" in orch_prompt
              and "never sign off with their name" in orch_prompt)


# ============================================================================
# 3. Mocked Tool Execution (send_email & reply_to_email)
# ============================================================================
async def test_tool_executions():
    print("\n--- 3. Mocked Tool Executions ---")

    user = config.UserContext(
        user_id=1,
        phone_number="+18322699252",
        name="Vinay",
        timezone="America/New_York",
        messa_email_local_part="vinay",
        is_admin=True,
    )

    from messa.approval import AutoApproveGate

    # Build tools for user with AutoApproveGate so destructive tools can run
    tools_list = personal_inbox_tools.build_personal_inbox_tools(user, AutoApproveGate())
    send_tool = next(t for t in tools_list if t.name == "send_email")
    reply_tool = next(t for t in tools_list if t.name == "reply_to_email")

    # A. Test send_email with accidental user sign-off
    with patch("messa.tools.personal_inbox_tools.resend_send_email", new_callable=AsyncMock) as mock_send:
        mock_send.return_value = {"id": "resend_123"}
        with patch.object(config, "RESEND_API_KEY", "re_test_dummy_key"):
            raw_body = "Hi team, please find our revised estimate.\n\nBest,\nVinay"
            result = await send_tool.coroutine(
                to="vendor@example.com",
                subject="Estimate Follow-up",
                body=raw_body,
            )
            check("send_email executes successfully", "Sent from vinay@textmessa.com" in result)
            check("send_email called resend_send_email exactly once", mock_send.call_count == 1)

            # Inspect actual body passed to Resend
            sent_args, sent_kwargs = mock_send.call_args
            sent_body = sent_args[4]
            sent_from_name = sent_kwargs.get("from_name")

            check("send_email automatically sanitized body before sending",
                  "Best,\nMessa (Assistant to Vinay)" in sent_body and "Best,\nVinay" not in sent_body)
            check("send_email passed correct from_name header",
                  sent_from_name == "Messa, personal assistant of Vinay")

    # B. Test reply_to_email with simulated database thread
    fake_inbound = {
        "from_address": "robocafe.business@gmail.com",
        "subject": "Meeting proposal next week",
        "message_id": "<inbound_msg_456@example.com>",
        "references_header": None,
        "auto_submitted": False,
    }

    with patch("messa.db.get_latest_inbound_message_in_thread", new_callable=AsyncMock) as mock_get_thread, \
         patch("messa.tools.personal_inbox_tools.resend_send_email", new_callable=AsyncMock) as mock_send, \
         patch.object(config, "RESEND_API_KEY", "re_test_dummy_key"):

        mock_get_thread.return_value = fake_inbound
        mock_send.return_value = {"id": "resend_456"}

        raw_reply = "Vinay asked me to confirm noon works.\n\nThanks,\nVinay"
        reply_result = await reply_tool.coroutine(
            thread_id="thread_abc",
            body=raw_reply,
            autonomous=False,
        )

        check("reply_to_email executes successfully", "Replied to robocafe.business@gmail.com" in reply_result)
        sent_args, sent_kwargs = mock_send.call_args
        sent_body = sent_args[4]
        sent_subject = sent_args[3]

        check("reply_to_email formatted reply subject as Re: ...", sent_subject == "Re: Meeting proposal next week")
        check("reply_to_email sanitized sign-off",
              "Thanks,\nMessa (Assistant to Vinay)" in sent_body)
        check("reply_to_email passed correct message-id headers",
              sent_kwargs.get("in_reply_to") == "<inbound_msg_456@example.com>")


# ============================================================================
# 4. Live Model Generation Verification (Does the LLM actually obey?)
# ============================================================================
async def test_live_llm_generation():
    print("\n--- 4. Live Model Prompt Generation Verification ---")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not openrouter_key or openrouter_key.startswith("sk-test-dummy"):
        print("[SKIP] Live LLM test skipped: no real OPENROUTER_API_KEY in environment.")
        return

    from langchain_core.messages import HumanMessage, SystemMessage

    user = config.UserContext(
        user_id=1,
        phone_number="+18322699252",
        name="Vinay",
        timezone="America/New_York",
        messa_email_local_part="vinay",
    )

    system_prompt = personal_inbox_tools.build_personal_inbox_system_prompt(user)
    llm = config.build_model(config.SUBAGENT_MODEL_NAME)

    test_scenarios = [
        (
            "Inbound from robocafe.business@gmail.com: 'Hey, checking to see if you are available Wednesday next week at noon for lunch.'\n"
            "User Vinay texted you: 'Yes tell them noon next Wednesday works.'\n"
            "Write the body text for reply_to_email.",
            "Lunch Meeting Confirmation",
        ),
        (
            "User Vinay texted you: 'Send an email to supplier@acme.com asking for a quote on 500 coffee cups.'\n"
            "Write the body text for send_email.",
            "Cold Vendor Quote Request",
        ),
    ]

    for prompt_text, scenario_label in test_scenarios:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=prompt_text),
        ]
        try:
            resp = await llm.ainvoke(messages)
            body = resp.content.strip()
            print(f"\nModel Output for '{scenario_label}':\n---\n{body}\n---")

            # Assertions on model's natural generation
            check(f"Live LLM [{scenario_label}]: speaks on behalf of Vinay in 3rd person",
                  ("Vinay" in body or "on behalf of" in body or "Mr. Salikineedi" in body))

            check(f"Live LLM [{scenario_label}]: does NOT ghostwrite as Vinay ('works for me' / 'my calendar')",
                  "works for me" not in body.lower() and "my calendar" not in body.lower())

            check(f"Live LLM [{scenario_label}]: does NOT sign off as 'Best, Vinay'",
                  not body.endswith("Vinay") and "Best, Vinay" not in body)

        except Exception as e:
            check(f"Live LLM [{scenario_label}] call succeeded", False, f"LLM error: {e}")


async def main():
    test_sanitizer_edge_cases()
    test_system_prompt_profiles()
    await test_tool_executions()
    await test_live_llm_generation()

    print("\n" + "=" * 60)
    if failures:
        print(f"FAILED: {len(failures)} check(s) failed: {failures}")
        sys.exit(1)
    else:
        print("SUCCESS: ALL RIGOROUS TESTS PASSED WITH ZERO ERRORS!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
