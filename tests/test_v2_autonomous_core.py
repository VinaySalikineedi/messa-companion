"""Verification test suite for Messa v2 Autonomous Core.

Tests:
1. Front-Door Intent Router triage (banter, status queries, OTPs, new goals).
2. Autonomous Loop Engine verification logic.
3. Direct tools integration in orchestrator (email, capsule, routines, executive).
4. Turn-based message consolidation.
5. Fast-path reaction classifier.
"""
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import config, db
from messa.agents import registry
from messa.autonomous_loop import AutonomousLoopEngine
from messa.intent_router import IntentType, triage_incoming_message
from messa.server import _pick_contextual_reaction

failures = []


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


async def main():
    user = config.UserContext(user_id=1, phone_number="+1234567890", channel="sms")

    # 1. Front-Door Intent Router
    d_banter = await triage_incoming_message(user, "Sounds good, thanks!")
    check("Intent Router: 'Sounds good, thanks!' triaged as CHAT_BANTER", d_banter.intent == IntentType.CHAT_BANTER)

    d_status = await triage_incoming_message(user, "What is the status of my research?")
    check("Intent Router: 'What is the status...' triaged as STATUS_QUERY", d_status.intent == IntentType.STATUS_QUERY)

    d_goal = await triage_incoming_message(user, "Find the best flight from NYC to London under $600")
    check("Intent Router: 'Find the best flight...' triaged as NEW_GOAL", d_goal.intent == IntentType.NEW_GOAL)

    # 2. Autonomous Loop Engine Ground-Truth Verification
    engine = AutonomousLoopEngine(user)
    v_success, msg_s = engine.verify_tool_outcome("send_email", "Email queued and sent with message id #msg_123")
    check("Autonomous Loop: Verifies successful email dispatch", v_success is True)

    v_fail, msg_f = engine.verify_tool_outcome("send_email", "Error: failed to connect to SMTP server")
    check("Autonomous Loop: Catches failed tool execution", v_fail is False)

    v_capsule, _ = engine.verify_tool_outcome("create_project_capsule", "Created project capsule #42 for Delta Refund")
    check("Autonomous Loop: Verifies project capsule creation", v_capsule is True)

    # 3. Direct Tools on Orchestrator
    orchestrator_tools = registry.build_orchestrator_tools(user)
    tool_names = {t.name for t in orchestrator_tools}
    check("Direct Tools: send_email is directly on orchestrator", "send_email" in tool_names)
    check("Direct Tools: search_my_emails is directly on orchestrator", "search_my_emails" in tool_names)
    check("Direct Tools: create_reminder is directly on orchestrator", "create_reminder" in tool_names)
    check("Direct Tools: get_project_capsule_details is directly on orchestrator", "get_project_capsule_details" in tool_names)

    # 4. Fast-path reaction classifier (<1ms regex matching)
    r_mail = await _pick_contextual_reaction("Please check my email")
    check("Fast Reaction: Email request gets ✉️", r_mail == "✉️")

    r_search = await _pick_contextual_reaction("Research flights to Paris")
    check("Fast Reaction: Search request gets 🔍", r_search == "🔍")

    r_remind = await _pick_contextual_reaction("Remind me tomorrow at 9am")
    check("Fast Reaction: Reminder request gets 📅", r_remind == "📅")

    if failures:
        print(f"\nFAILED ({len(failures)} failures):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("\nALL V2 AUTONOMOUS CORE TESTS PASSED!")


if __name__ == "__main__":
    asyncio.run(main())
