"""Comprehensive Unit & Regression Test Suite for light-web-agent v2.2.

Tests all 8 core architectural pillars:
1. Persistent Session & Suspended State (Zero-Kill OTP).
2. Perception Engine & Deep Frame Traversal.
3. Zero-LLM Web Convention Prior (Landmark Pass, Direct-URLs, Dark Patterns).
4. Macro-Action Batching & Staleness Signature Validation.
5. Security: Credential Tokenization & Intent-Alignment Critic.
6. Generalized Human Checkpoints (OTP, CAPTCHA, Risk Review).
7. Circuit Breakers: Step caps, exact-repeat detection, oscillation detection.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Ensure required test dummy env vars are set
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa.channels.browser_session_manager import (
    BrowserSessionManager,
    ManagedBrowserSession,
    PopupOriginPolicy,
)
from messa.channels.light_web_perception import (
    CANONICAL_INTENTS,
    PerceptionEngine,
    WebConventionPrior,
    compute_target_signature,
)
from messa.channels.light_web_agent import (
    AgentDecision,
    HumanCheckpoint,
    IntentAlignmentCheck,
    LightWebAgent,
    MacroAction,
    MacroStep,
)

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ===========================================================================
# 1. Session Manager & Popup Origin Policy Tests
# ===========================================================================

def test_popup_origin_policy():
    print("\n--- TEST: Popup Origin Policy ---")
    policy = PopupOriginPolicy(
        allowlisted_origins=["accounts.google.com", "appleid.apple.com", "cardinalcommerce.com"]
    )

    # Allowlisted external identity providers
    check("Google SSO allowed", policy.is_allowed("https://accounts.google.com/o/oauth2/auth"))
    check("Apple ID allowed", policy.is_allowed("https://appleid.apple.com/auth/authorize"))
    check("3D Secure bank gateway allowed", policy.is_allowed("https://acs.cardinalcommerce.com/verify"))

    # Parent domain matching (same registrable domain)
    check("Parent same domain allowed", policy.is_allowed("https://auth.walmart.com/login", "https://www.walmart.com/cart"))
    check("Parent sub-domain allowed", policy.is_allowed("https://checkout.instacart.com", "https://www.instacart.com/store"))

    # Non-allowlisted external domain (phishing / ad popup)
    check("Unknown phishing popup rejected", not policy.is_allowed("https://evil-phish.com/login", "https://www.walmart.com"))
    check("Untrusted tracking popup rejected", not policy.is_allowed("https://track.adnetwork.io", "https://www.instacart.com"))


async def test_session_suspension_and_keepalive():
    print("\n--- TEST: Session Suspension & Keepalive ---")
    mgr = BrowserSessionManager(default_ttl_seconds=10, keepalive_interval=1)

    # Create dummy session
    session = ManagedBrowserSession(
        session_id="test_sess_001",
        provider="browserbase",
        cdp_url="wss://test.browserbase.com",
        user_id=123,
    )
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.evaluate = AsyncMock(return_value=2)
    session.active_page = mock_page

    mgr._sessions["test_sess_001"] = session
    mgr._user_to_session[123] = "test_sess_001"

    # 1. Suspend session with OTP checkpoint
    checkpoint_data = {
        "kind": "otp",
        "prompt_to_user": "Enter 6-digit code",
        "timeout_seconds": 300,
    }
    await mgr.suspend_session("test_sess_001", checkpoint_data)

    check("Session status is SUSPENDED_WAITING_INPUT", session.status == "SUSPENDED_WAITING_INPUT")
    check("Keepalive task created", session.keepalive_task is not None and not session.keepalive_task.done())

    # Wait for keepalive ping to execute
    await asyncio.sleep(1.2)
    check("Keepalive page.evaluate was called", mock_page.evaluate.called)

    # 2. Resume session with SMS OTP
    resumed = await mgr.resume_session("test_sess_001", human_input="948201")
    await asyncio.sleep(0.05) # Allow event loop tick for task cancellation
    check("Session resumed successfully", resumed is not None and resumed.status == "ACTIVE")
    check("Human input recorded", resumed.checkpoint.get("human_input") == "948201")
    check("Keepalive task stopped on resume", session.keepalive_task.done())

    # Cleanup
    with patch("messa.channels.browser.release_session", new_callable=AsyncMock):
        await mgr.release_session("test_sess_001")
    check("Session released and terminated", session.status == "TERMINATED")


# ===========================================================================
# 2. Perception Engine & Target Signature Tests
# ===========================================================================

def test_target_signature_computation():
    print("\n--- TEST: Target Signature Computation ---")
    sig1 = compute_target_signature("button", "Claim $0 Delivery Fee", "button")
    sig2 = compute_target_signature("button", "Claim $0 Delivery Fee", "button")
    sig3 = compute_target_signature("button", "Checkout", "button")

    check("Identical elements produce identical signatures", sig1 == sig2)
    check("Different elements produce different signatures", sig1 != sig3)
    check("Signature is 16-character hex string", len(sig1) == 16)


def test_web_convention_prior():
    print("\n--- TEST: Web Convention Prior Landmark & Dark-Pattern Matching ---")
    elements = [
        {"id": "e1", "role": "searchbox", "name": "Search products and groceries", "signature": "sig_search"},
        {"id": "e2", "role": "link", "name": "Cart (3 items)", "signature": "sig_cart"},
        {"id": "e3", "role": "button", "name": "Sign In / Register", "signature": "sig_signin"},
        {
            "id": "e4",
            "role": "input",
            "name": "Add 2-Year Protection Plan (+$14.99)",
            "signature": "sig_plan",
            "dark_pattern": True,
        },
        {
            "id": "e5",
            "role": "button",
            "name": "No thanks, I don't want to save money",
            "signature": "sig_decline",
            "dark_pattern": False,
        },
    ]

    # Test Canonical Landmark matches
    search_match = WebConventionPrior.match_canonical_landmark("search", elements)
    check("Matched searchbox landmark", search_match is not None and search_match["id"] == "e1")

    cart_match = WebConventionPrior.match_canonical_landmark("cart", elements)
    check("Matched cart landmark", cart_match is not None and cart_match["id"] == "e2")

    signin_match = WebConventionPrior.match_canonical_landmark("sign_up", elements)
    check("Matched sign_up landmark", signin_match is not None and signin_match["id"] == "e3")

    # Test Dark Pattern Detection
    dark_patterns = WebConventionPrior.detect_dark_patterns(elements)
    check("Detected both pre-checked fee and guilt-trip copy", len(dark_patterns) == 2)
    flagged_ids = [dp["id"] for dp in dark_patterns]
    check("Flagged e4 (pre-checked paid plan)", "e4" in flagged_ids)
    check("Flagged e5 (guilt-trip decline button)", "e5" in flagged_ids)


# ===========================================================================
# 3. Macro-Action Batching & Staleness Guard Tests
# ===========================================================================

async def test_macro_action_staleness_guard():
    print("\n--- TEST: Macro-Action Staleness Signature Guard ---")
    session = ManagedBrowserSession("sess_test", "browserbase", "wss://fake")
    mock_page = MagicMock()
    mock_locator = MagicMock()
    mock_locator.fill = AsyncMock()
    mock_locator.click = AsyncMock()
    mock_locator.first = mock_locator
    mock_page.locator.return_value = mock_locator
    session.active_page = mock_page

    agent = LightWebAgent(session, user_goal="Fill checkout form")

    # Elements currently on the page
    elem_map = {
        "e1": {"id": "e1", "name": "First Name", "signature": "sig_valid_1", "selector": "#fname"},
        "e2": {"id": "e2", "name": "Last Name", "signature": "sig_valid_2", "selector": "#lname"},
    }

    # MacroAction with matching signature
    valid_action = MacroAction(steps=[
        MacroStep(action="type", target_id="e1", target_signature="sig_valid_1", value="Alice"),
        MacroStep(action="type", target_id="e2", target_signature="sig_valid_2", value="Smith"),
    ])

    success, err = await agent._execute_macro_action(mock_page, valid_action, elem_map)
    check("Valid macro-action batch executed", success is True and err is None)

    # MacroAction where an unexpected modal popped up and changed signature of e2
    stale_action = MacroAction(steps=[
        MacroStep(action="type", target_id="e1", target_signature="sig_valid_1", value="Alice"),
        MacroStep(action="type", target_id="e2", target_signature="OLD_STALE_SIG", value="Smith"),
    ])

    success, err = await agent._execute_macro_action(mock_page, stale_action, elem_map)
    check("Staleness Guard aborted execution on mismatched signature", success is False)
    check("Error mentions signature mismatch", "signature mismatch" in (err or "").lower())


# ===========================================================================
# 4. Credential Tokenization & Vault Resolution
# ===========================================================================

async def test_credential_tokenization():
    print("\n--- TEST: Credential Tokenization ({{cred:...}}) ---")
    session = ManagedBrowserSession("sess_test", "browserbase", "wss://fake")
    mock_page = MagicMock()
    mock_locator = MagicMock()
    mock_locator.fill = AsyncMock()
    mock_locator.first = mock_locator
    mock_page.locator.return_value = mock_locator
    session.active_page = mock_page

    credentials = {
        "walmart_email": "shopper@messa.ai",
        "walmart_password": "UltraSecretPassword123!",
    }

    agent = LightWebAgent(session, user_goal="Sign in", credentials=credentials)
    elem_map = {
        "e1": {"id": "e1", "name": "Password Input", "signature": "sig_pw", "selector": "#password"},
    }

    token_action = MacroAction(steps=[
        MacroStep(action="type", target_id="e1", target_signature="sig_pw", value="{{cred:walmart_password}}"),
    ])

    await agent._execute_macro_action(mock_page, token_action, elem_map)
    check("Password fill was called on locator", mock_locator.fill.called)
    called_value = mock_locator.fill.call_args[0][0]
    check("Token was replaced with actual secret locally", called_value == "UltraSecretPassword123!")


# ===========================================================================
# 5. Circuit Breakers: Oscillation & Step Cap Detection
# ===========================================================================

def test_circuit_breakers():
    print("\n--- TEST: Circuit Breakers (Step Cap & Oscillation Detection) ---")
    session = ManagedBrowserSession("sess_test", "browserbase", "wss://fake")
    agent = LightWebAgent(session, user_goal="Navigate items", max_steps=10)

    # 1. Step Cap
    agent.step_history = [{}] * 10
    decision = agent._check_circuit_breakers()
    check("Step cap tripped at 10 steps", decision is not None and decision.status == "FAILED")
    check("Summary mentions max step limit", "step limit" in (decision.result_summary or "").lower())

    # 2. Oscillation Detection (A -> B -> A -> B)
    agent.step_history = [{}] * 4
    agent.action_history_signatures = ["click:e1:sigA", "click:e2:sigB", "click:e1:sigA", "click:e2:sigB"]
    decision = agent._check_circuit_breakers()
    check("Oscillation detector caught A-B-A-B loop", decision is not None and decision.status == "FAILED")
    check("Thought notes oscillating loop", "oscillating" in (decision.thought or "").lower())


# ===========================================================================
# 6. Human Checkpoint OTP & CAPTCHA Detection
# ===========================================================================

def test_human_checkpoint_detection():
    print("\n--- TEST: Human Checkpoint Detection ---")
    session = ManagedBrowserSession("sess_test", "browserbase", "wss://fake", live_view_url="https://live.browserbase.com/view123")
    agent = LightWebAgent(session, user_goal="Sign up on Walmart")

    # 1. SMS OTP detection
    a11y_otp = (
        "[e1] heading \"Verification Required\"\n"
        "[e2] text \"Enter the 6-digit code sent to (555) 382-9102 to verify your account\"\n"
        "[e3] textbox \"Verification Code\""
    )
    checkpoint = agent._detect_human_checkpoint("https://www.walmart.com/verify", "Verification", a11y_otp)
    check("Detected SMS OTP checkpoint", checkpoint is not None and checkpoint.kind == "otp")
    check("Extracted phone number in prompt", "(555) 382-9102" in checkpoint.prompt_to_user)

    # 2. CAPTCHA detection
    a11y_captcha = (
        "[e1] heading \"Robot or human?\"\n"
        "[e2] button \"Press & Hold to confirm you are human\""
    )
    checkpoint = agent._detect_human_checkpoint("https://www.walmart.com/blocked", "Robot or human?", a11y_captcha)
    check("Detected CAPTCHA challenge", checkpoint is not None and checkpoint.kind == "captcha")
    check("Attached interactive live view link", checkpoint.interactive_url == "https://live.browserbase.com/view123")


# ===========================================================================
# 7. Intent-Alignment Critic Isolation Test
# ===========================================================================

async def test_intent_alignment_critic_consequential():
    print("\n--- TEST: Intent-Alignment Critic Guard ---")
    session = ManagedBrowserSession("sess_test", "browserbase", "wss://fake")
    agent = LightWebAgent(session, user_goal="Order 2 gallons of organic whole milk")

    # Normal order action
    normal_action = MacroAction(
        steps=[MacroStep(action="click", target_id="e1", target_signature="sig1")],
        is_consequential=True,
    )

    with patch("messa.config.build_model") as mock_build_model:
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content='{"verdict": "aligned", "reason": "Purchasing milk as requested"}')
        mock_build_model.return_value = mock_model

        is_aligned, reason = await agent._run_intent_alignment_critic(normal_action)
        check("Critic confirmed aligned action", is_aligned is True)

        # Injected prompt hijack trying to buy an iPhone
        mock_model.ainvoke.return_value = MagicMock(content='{"verdict": "misaligned", "reason": "Action attempts to purchase an iPhone instead of requested milk"}')
        is_aligned, reason = await agent._run_intent_alignment_critic(normal_action)
        check("Critic caught misaligned/injected action", is_aligned is False)
        check("Reason flags the misalignment", "iPhone" in reason)


async def test_scratchpad_and_skills_integration():
    print("\n--- TEST: Scratchpad & Skills Playbook Integration ---")
    session = ManagedBrowserSession(session_id="test_sess", provider="browserbase", cdp_url="ws://dummy")
    agent = LightWebAgent(session=session, user_goal="Add 5 grocery items", user_id=999)

    # 1. Mock DB active task & skills
    mock_task = {"task_id": "task_abc_123", "task_type": "light_web_agent", "artifacts": {"cart_items": ["apples"]}}
    mock_skills = [{"solution_recipe": "Click search input directly on instacart."}]

    with patch("messa.db.get_active_task", new=AsyncMock(return_value=mock_task)), \
         patch("messa.db.search_skills", new=AsyncMock(return_value=mock_skills)), \
         patch("messa.db.update_active_task_artifacts", new=AsyncMock(return_value={"artifacts": {"cart_items": ["apples", "milk"]}})), \
         patch("messa.db.upsert_skill", new=AsyncMock(return_value={"success_count": 1})):

        await agent._init_scratchpad_and_skills("instacart.com")
        check("Active task ID resolved", agent.active_task_id == "task_abc_123")
        check("Scratchpad artifacts loaded", agent.scratchpad_artifacts.get("cart_items") == ["apples"])
        check("Domain skills loaded", "Click search input directly on instacart." in agent.domain_skills)

        # 2. Update scratchpad
        await agent._update_scratchpad({"cart_items": ["apples", "milk"]})
        check("Scratchpad artifacts updated in memory", agent.scratchpad_artifacts.get("cart_items") == ["apples", "milk"])

        # 3. Persist skill
        await agent._persist_skill("instacart.com", "signup_modal", "Wait 2s for modal animation.")
        from messa.channels.light_web_agent import GLOBAL_WEB_SKILL_CACHE
        check("Skill cached in memory", GLOBAL_WEB_SKILL_CACHE.get("instacart.com", {}).get("recipe") == "Wait 2s for modal animation.")


def test_live_activity_dispatch():
    print("\n--- TEST: Deepsearch Live Activity Pipeline ---")
    from messa import live_activity
    test_uid = 888777

    # 1. Start live activity
    live_activity.start(test_uid, "Test live task")
    check("Live activity started", test_uid in live_activity._state)
    check("Initial description set", live_activity._state[test_uid]["description"] == "Test live task")

    # 2. Session ID & URL
    live_activity.set_session_id(test_uid, "sess_bb_123")
    live_activity.set_url(test_uid, "https://www.instacart.com/store")
    check("Session ID set", live_activity._state[test_uid]["bb_session_id"] == "sess_bb_123")
    check("Page URL set", live_activity._state[test_uid]["url"] == "https://www.instacart.com/store")

    # 3. Step logging
    live_activity.add_step(test_uid, "Turn 1: Clicked searchbox")
    live_activity.add_step(test_uid, "Turn 2: Typed bananas")
    check("Steps recorded", len(live_activity._state[test_uid]["steps"]) == 2)

    # 4. Human checkpoint banner
    live_activity.set_waiting_for_human(test_uid, "Please enter SMS verification code")
    check("Waiting for human banner active", live_activity._state[test_uid]["waiting_for_human"] == "Please enter SMS verification code")
    live_activity.clear_waiting_for_human(test_uid)
    check("Waiting for human cleared", live_activity._state[test_uid]["waiting_for_human"] is None)

    # 5. Clean teardown
    live_activity.set_closing(test_uid)
    check("Closing state set", live_activity._state[test_uid]["closing"] is True)
    live_activity.clear(test_uid)
    check("Live activity cleared", test_uid not in live_activity._state)


async def main():
    print("=== RUNNING LIGHT-WEB-AGENT SUITE ===")
    test_popup_origin_policy()
    await test_session_suspension_and_keepalive()
    test_target_signature_computation()
    test_web_convention_prior()
    await test_macro_action_staleness_guard()
    await test_credential_tokenization()
    test_circuit_breakers()
    test_human_checkpoint_detection()
    await test_intent_alignment_critic_consequential()
    await test_scratchpad_and_skills_integration()
    test_live_activity_dispatch()

    print("\n=====================================")
    if failures:
        print(f"FAILED ({len(failures)} failures):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED (100% GREEN)!")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())

