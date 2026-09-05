"""Tests for Messa's executive assistant voice & persona across outbound emails
sent from the user's Messa address (@textmessa.com).
"""
import sys

from messa import config
from messa.agents import registry
from messa.tools import personal_inbox_tools

failures = []


def check(label: str, cond: bool):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def test_sanitize_assistant_email_body():
    user_name = "Vinay"

    # 1. Impersonated trailing sign-off gets corrected
    raw1 = (
        "Hey! Noon next Wednesday works great for me. Looking forward to catching up over lunch!\n\n"
        "Best,\n"
        "Vinay"
    )
    res1 = personal_inbox_tools._sanitize_assistant_email_body(raw1, user_name)
    check("Sanitizer corrects 'Best,\\nVinay' to Messa assistant sign-off",
          res1.splitlines()[-1] == "Messa (Assistant to Vinay)")

    # 2. Trailing dash sign-off gets corrected
    raw2 = "Attached is the document.\n\n- Vinay"
    res2 = personal_inbox_tools._sanitize_assistant_email_body(raw2, user_name)
    check("Sanitizer corrects '- Vinay' to Messa assistant sign-off",
          "Messa (Assistant to Vinay)" in res2)

    # 3. Trailing 'Thanks,\nVinay'
    raw3 = "Thanks for sending that over!\n\nThanks,\nVinay"
    res3 = personal_inbox_tools._sanitize_assistant_email_body(raw3, user_name)
    check("Sanitizer corrects 'Thanks,\\nVinay'", "Messa (Assistant to Vinay)" in res3)

    # 4. Legitimate assistant sign-off is left untouched
    raw4 = "Vinay asked me to confirm noon next Wednesday works.\n\nBest regards,\nMessa"
    res4 = personal_inbox_tools._sanitize_assistant_email_body(raw4, user_name)
    check("Sanitizer leaves valid assistant sign-off untouched", res4 == raw4)

    # 5. User name appearing in mid-sentence is NOT altered
    raw5 = "Hi Sarah, Vinay is traveling this afternoon but asked me to confirm the call."
    res5 = personal_inbox_tools._sanitize_assistant_email_body(raw5, user_name)
    check("Sanitizer does not alter user name mentioned mid-sentence", res5 == raw5)

    # 6. User with no name set does not crash
    res6 = personal_inbox_tools._sanitize_assistant_email_body(raw1, None)
    check("Sanitizer gracefully handles None user_name", res6 == raw1.strip())


def test_system_prompts():
    user = config.UserContext(
        user_id=1,
        phone_number="+18322699252",
        name="Vinay",
        timezone="America/New_York",
        messa_email_local_part="vinay",
    )

    inbox_prompt = personal_inbox_tools.build_personal_inbox_system_prompt(user)
    check("personal_inbox_agent prompt carries STRICT EXECUTIVE ASSISTANT RULE",
          "VOICE & PERSONA IN EMAILS (STRICT EXECUTIVE ASSISTANT RULE)" in inbox_prompt)
    check("personal_inbox_agent prompt mentions writing on behalf of user",
          "I'm reaching out on behalf of Vinay" in inbox_prompt)
    check("personal_inbox_agent prompt forbids first-person impersonation",
          "NEVER write in the first person pretending to be the user" in inbox_prompt)
    check("personal_inbox_agent prompt forbids user name sign-off",
          "NEVER sign off with the user's name" in inbox_prompt)

    # Nameless user fallback
    nameless_user = config.UserContext(
        user_id=2,
        phone_number="+18322699253",
        name=None,
        timezone="America/New_York",
        messa_email_local_part="user2",
    )
    nameless_prompt = personal_inbox_tools.build_personal_inbox_system_prompt(nameless_user)
    check("Nameless user falls back cleanly to 'the user'",
          "personal assistant to the user" in nameless_prompt)

    # Orchestrator prompt
    orch_prompt = registry._build_system_prompt(user)
    check("Orchestrator prompt carries assistant voice instruction for personal_inbox_agent",
          "Messa ALWAYS writes as the user's executive assistant on their behalf" in orch_prompt)


def main():
    test_sanitize_assistant_email_body()
    test_system_prompts()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL ASSISTANT VOICE TESTS PASSED!")


if __name__ == "__main__":
    main()
