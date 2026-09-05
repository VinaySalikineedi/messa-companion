"""Tests for Messa's executive assistant voice & persona across outbound emails
sent from the user's Messa address (@textmessa.com).
"""
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import config  # noqa: E402
from messa.agents import registry  # noqa: E402
from messa.tools import personal_inbox_tools  # noqa: E402

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

    # Post-compression (plans/glowing-forging-pumpkin.md Part 3): the old
    # 4-bullet "VOICE & PERSONA IN EMAILS (STRICT EXECUTIVE ASSISTANT RULE)"
    # block is now one short paragraph -- same rules (third person, no
    # first-person impersonation, no name sign-off), reworded/compressed.
    inbox_prompt = personal_inbox_tools.build_personal_inbox_system_prompt(user)
    check("personal_inbox_agent prompt carries the voice-in-the-body rule",
          "Voice in the body: you're Messa, Vinay's assistant, writing on their behalf" in inbox_prompt)
    check("personal_inbox_agent prompt gives a concrete third-person example",
          "Vinay asked me to follow up" in inbox_prompt)
    check("personal_inbox_agent prompt forbids first-person impersonation",
          "never first-person as Vinay" in inbox_prompt)
    check("personal_inbox_agent prompt forbids user name sign-off",
          "never sign off with Vinay's name" in inbox_prompt)

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
          "Messa, the user's assistant" in nameless_prompt)

    # Orchestrator prompt: post-compression, this lives in the shared
    # email/calendar/tasks "Routing --" paragraph's email bullet now, not a
    # standalone assistant-voice sentence.
    orch_prompt = registry._build_system_prompt(user)
    check("Orchestrator prompt tells Messa to write third-person on the user's behalf "
          "from their Messa address",
          "always write third-person on their behalf" in orch_prompt
          and "never sign off with their name" in orch_prompt)


def main():
    test_sanitize_assistant_email_body()
    test_system_prompts()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL ASSISTANT VOICE TESTS PASSED!")


if __name__ == "__main__":
    main()
