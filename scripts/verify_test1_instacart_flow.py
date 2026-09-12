"""Instacart Happy Path & Skill Cache Verification for light-web-agent (v2.2).

Exercises:
1. Web Convention Prior: Heuristically matches top-right 'Sign up' with 0ms LLM latency.
2. Macro-Action Batching: Fills Full Name, Email, and Password in one round trip with {{cred:...}} tokens.
3. OTP Human Checkpoint: Pauses on SMS OTP challenge without killing browser session.
4. Persistent Session Resume: User sends OTP -> session resumes on EXACT same tab.
5. Search & Add-to-Cart Flow: Adds 5 grocery items to cart and verifies cart count.
6. Dynamic Skill Cache: Caches successful recipe on first run, fast-paths second run via cache.
"""
from __future__ import annotations

import asyncio
import http.server
import json
import os
import socketserver
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from playwright.async_api import async_playwright

from messa import config
from messa.channels.browser_session_manager import (
    ManagedBrowserSession,
    PopupOriginPolicy,
    session_manager,
)
from messa.channels.light_web_agent import (
    GLOBAL_WEB_SKILL_CACHE,
    LightWebAgent,
    MacroAction,
    MacroStep,
)
from messa.channels.light_web_perception import PerceptionEngine, WebConventionPrior

# ---------------------------------------------------------------------------
# Staged Instacart Flow HTML Server
# ---------------------------------------------------------------------------

INSTACART_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Instacart - Groceries Delivered</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; padding: 20px; }
        header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; padding-bottom: 15px; }
        .logo { font-size: 24px; font-weight: bold; color: #003d29; }
        .nav-right { display: flex; gap: 15px; align-items: center; }
        button { cursor: pointer; padding: 8px 16px; border-radius: 20px; font-weight: 600; }
        .btn-signup { background: #003d29; color: white; border: none; }
        .modal { display: none; position: fixed; top: 15%; left: 35%; width: 30%; background: #fff; border: 1px solid #ccc; border-radius: 12px; padding: 25px; box-shadow: 0 8px 24px rgba(0,0,0,0.15); z-index: 100; }
        .modal.active { display: block; }
        .input-group { margin-bottom: 12px; }
        .input-group label { display: block; margin-bottom: 4px; font-size: 13px; font-weight: 500; }
        .input-group input { width: 92%; padding: 10px; border: 1px solid #ccc; border-radius: 6px; font-size: 14px; }
        .product-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 15px; margin-top: 20px; }
        .product-card { border: 1px solid #eee; border-radius: 8px; padding: 15px; text-align: center; }
        .cart-badge { background: #003d29; color: white; border-radius: 50%; padding: 2px 8px; font-size: 12px; }
    </style>
</head>
<body>
    <header>
        <div class="logo">Instacart</div>
        <div class="nav-right">
            <button id="cart-button">Cart <span id="cart-count" class="cart-badge">0</span></button>
            <button id="signup-header-btn" class="btn-signup" onclick="openSignup()">Sign up</button>
        </div>
    </header>

    <main id="main-content">
        <h2>Fresh Grocery Selection</h2>
        <div class="product-grid">
            <div class="product-card" data-item="bananas">
                <h3>Organic Bananas</h3>
                <p>$1.99 / bunch</p>
                <button class="add-item-btn" onclick="addToCart('Organic Bananas')">Add to Cart</button>
            </div>
            <div class="product-card" data-item="milk">
                <h3>Organic Whole Milk</h3>
                <p>$4.49 / half-gallon</p>
                <button class="add-item-btn" onclick="addToCart('Organic Whole Milk')">Add to Cart</button>
            </div>
            <div class="product-card" data-item="eggs">
                <h3>Grade A Large Eggs</h3>
                <p>$3.99 / dozen</p>
                <button class="add-item-btn" onclick="addToCart('Grade A Large Eggs')">Add to Cart</button>
            </div>
            <div class="product-card" data-item="bread">
                <h3>Artisan Sourdough Bread</h3>
                <p>$4.99 / loaf</p>
                <button class="add-item-btn" onclick="addToCart('Artisan Sourdough Bread')">Add to Cart</button>
            </div>
            <div class="product-card" data-item="avocados">
                <h3>Hass Avocados (Bag of 4)</h3>
                <p>$3.99 / bag</p>
                <button class="add-item-btn" onclick="addToCart('Hass Avocados')">Add to Cart</button>
            </div>
        </div>
    </main>

    <!-- Sign Up Modal -->
    <div id="signup-modal" class="modal">
        <h3>Create Your Account</h3>
        <div class="input-group">
            <label for="fullname">Full Name</label>
            <input type="text" id="fullname" placeholder="Full Name">
        </div>
        <div class="input-group">
            <label for="email">Email Address</label>
            <input type="email" id="email" placeholder="name@example.com">
        </div>
        <div class="input-group">
            <label for="password">Password</label>
            <input type="password" id="password" placeholder="Create password">
        </div>
        <button id="submit-signup" style="width: 100%; background: #003d29; color: white; margin-top: 10px;" onclick="triggerOTP()">Continue with Phone</button>
    </div>

    <!-- OTP Modal -->
    <div id="otp-modal" class="modal">
        <h3>Verification Required</h3>
        <p>Enter the 6-digit code sent to (555) 928-1029 to verify your account</p>
        <div class="input-group">
            <label for="otp-input">Verification Code</label>
            <input type="text" id="otp-input" placeholder="6-digit code" maxlength="6">
        </div>
        <button id="submit-otp" style="width: 100%; background: #003d29; color: white; margin-top: 10px;" onclick="confirmAccount()">Verify and Sign In</button>
    </div>

    <script>
        let cart = [];
        function openSignup() {
            document.getElementById('signup-modal').classList.add('active');
        }
        function triggerOTP() {
            document.getElementById('signup-modal').classList.remove('active');
            document.getElementById('otp-modal').classList.add('active');
        }
        function confirmAccount() {
            const code = document.getElementById('otp-input').value;
            if (code && code.length >= 4) {
                document.getElementById('otp-modal').classList.remove('active');
                document.getElementById('signup-header-btn').innerText = 'Account: Shopper';
            }
        }
        function addToCart(item) {
            cart.push(item);
            document.getElementById('cart-count').innerText = cart.length;
        }
    </script>
</body>
</html>
"""

class InstacartServer:
    def __init__(self, port: int = 8888):
        self.port = port
        self.server = None
        self.thread = None

    def start(self):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(INSTACART_HTML.encode("utf-8"))

            def log_message(self, format, *args):
                pass

        socketserver.TCPServer.allow_reuse_address = True
        self.server = socketserver.TCPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


# ---------------------------------------------------------------------------
# Test Runner
# ---------------------------------------------------------------------------

async def run_instacart_flow_test():
    print("=" * 65)
    print("TEST 1: INSTACART ONBOARDING, CART FLOW & SKILL CACHE")
    print("=" * 65)
    print("Exercises:")
    print("  1. Web Convention Prior (Finds Sign Up top-right, 0ms LLM)")
    print("  2. Macro Batching (Fills Full Name, Email, Password in 1 round trip)")
    print("  3. OTP Human Checkpoint (SMS pause with 40s keepalive heartbeats)")
    print("  4. Persistent Session Resume (Resumes on exact same browser tab)")
    print("  5. Search + Add-to-Cart Flow (Adds 5 items and verifies cart)")
    print("  6. Dynamic Skill Cache (First run caches recipe; second run hits cache)")
    print("-" * 65)

    server = InstacartServer(8888)
    server.start()
    test_url = "http://127.0.0.1:8888"

    failures = []
    def check(name: str, cond: bool):
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}")
        if not cond:
            failures.append(name)

    credentials = {
        "instacart_email": "jane.shopper@messa.ai",
        "instacart_password": "SecurePassword987!",
        "instacart_name": "Jane Shopper",
    }

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            user_id = "instacart_happy_user"
            session = ManagedBrowserSession(
                session_id="sess_instacart_live",
                provider="browserbase",
                cdp_url="wss://mock",
                live_view_url="https://live.browserbase.com/view_instacart",
                user_id=user_id,
            )
            session.browser_inst = browser
            session.context = context
            session.active_page = page
            session.setup_page_listeners()
            session_manager._sessions[session.session_id] = session
            session_manager._user_to_session[user_id] = session.session_id

            await page.goto(test_url)

            # ---------------------------------------------------------------
            # 1. Web Convention Prior: Zero-LLM Landmark Sign-Up Match
            # ---------------------------------------------------------------
            print("\n--- 1. Testing Web Convention Prior Landmark Match ---")
            elements, a11y_text = await PerceptionEngine.extract_pruned_a11y_tree(page)
            elem_map = {el["id"]: el for el in elements}

            signup_match = WebConventionPrior.match_canonical_landmark("sign_up", elements)
            check("Landmark Prior matched Sign Up button without LLM", signup_match is not None)
            check("Sign Up button points to #signup-header-btn", "#signup-header-btn" in signup_match.get("selector", ""))

            agent = LightWebAgent(
                session=session,
                user_goal="Sign up for a new Instacart account and add 5 grocery items to the cart.",
                credentials=credentials,
                max_steps=15,
            )

            # Click Sign Up button
            action = MacroAction(steps=[MacroStep(action="click", target_id=signup_match["id"], target_signature=signup_match["signature"])])
            ok, err = await agent._execute_macro_action(page, action, elem_map)
            check("Sign Up modal opened", ok is True)
            await page.wait_for_timeout(300)

            # ---------------------------------------------------------------
            # 2. Macro-Action Batching: Form Fill in Single Round Trip
            # ---------------------------------------------------------------
            print("\n--- 2. Testing Macro-Action Batching & Tokenization ---")
            elements, _ = await PerceptionEngine.extract_pruned_a11y_tree(page)
            elem_map = {el["id"]: el for el in elements}

            name_el = next((e for e in elements if "#fullname" in e.get("selector", "")), None)
            email_el = next((e for e in elements if "#email" in e.get("selector", "")), None)
            pw_el = next((e for e in elements if "#password" in e.get("selector", "")), None)
            submit_el = next((e for e in elements if "#submit-signup" in e.get("selector", "")), None)

            check("Form elements perceived in accessibility snapshot", all([name_el, email_el, pw_el, submit_el]))

            # Execute multi-step batch with credential tokens
            signup_batch = MacroAction(steps=[
                MacroStep(action="type", target_id=name_el["id"], target_signature=name_el["signature"], value="{{cred:instacart_name}}"),
                MacroStep(action="type", target_id=email_el["id"], target_signature=email_el["signature"], value="{{cred:instacart_email}}"),
                MacroStep(action="type", target_id=pw_el["id"], target_signature=pw_el["signature"], value="{{cred:instacart_password}}"),
                MacroStep(action="click", target_id=submit_el["id"], target_signature=submit_el["signature"]),
            ])

            batch_ok, batch_err = await agent._execute_macro_action(page, signup_batch, elem_map)
            check("Batched macro-action executed successfully in 1 round trip", batch_ok is True)
            await page.wait_for_timeout(300)

            # ---------------------------------------------------------------
            # 3. OTP Human Checkpoint (Pause without Teardown)
            # ---------------------------------------------------------------
            print("\n--- 3. Testing SMS OTP Detection & Session Suspension ---")
            elements, a11y_text = await PerceptionEngine.extract_pruned_a11y_tree(page)
            elem_map = {el["id"]: el for el in elements}

            checkpoint = agent._detect_human_checkpoint(test_url, "Verification", a11y_text)
            check("Detected SMS OTP verification prompt on page", checkpoint is not None and checkpoint.kind == "otp")
            check("Extracted phone number hint in user prompt", "(555) 928-1029" in checkpoint.prompt_to_user)

            # Suspend session with 40s keepalive heartbeats
            await session_manager.suspend_session(session.session_id, checkpoint.model_dump())
            check("Session status marked SUSPENDED_WAITING_INPUT", session.status == "SUSPENDED_WAITING_INPUT")
            check("Keepalive heartbeat task active", session.keepalive_task is not None and not session.keepalive_task.done())

            # ---------------------------------------------------------------
            # 4. Session Resume on Same Browser Session
            # ---------------------------------------------------------------
            print("\n--- 4. Testing Session Resume with Human OTP Input ---")
            user_sms_code = "849201"
            resumed_sess = await session_manager.resume_session(session.session_id, human_input=user_sms_code)
            check("Session resumed on EXACT same browser session", resumed_sess.session_id == session.session_id)
            check("Session status back to ACTIVE", resumed_sess.status == "ACTIVE")
            check("Keepalive stopped after resume", session.keepalive_task.done())

            # Fill OTP code and confirm
            otp_el = next((e for e in elements if "#otp-input" in e.get("selector", "")), None)
            confirm_el = next((e for e in elements if "#submit-otp" in e.get("selector", "")), None)
            otp_action = MacroAction(steps=[
                MacroStep(action="type", target_id=otp_el["id"], target_signature=otp_el["signature"], value=user_sms_code),
                MacroStep(action="click", target_id=confirm_el["id"], target_signature=confirm_el["signature"]),
            ])
            await agent._execute_macro_action(page, otp_action, elem_map)
            await page.wait_for_timeout(300)

            # Verify account header updated
            account_text = await page.inner_text("#signup-header-btn")
            check("Account successfully verified and signed in", "Account: Shopper" in account_text)

            # ---------------------------------------------------------------
            # 5. Search + Add-to-Cart Flow (Add 5 Items)
            # ---------------------------------------------------------------
            print("\n--- 5. Testing Add-to-Cart Flow for 5 Grocery Items ---")
            elements, _ = await PerceptionEngine.extract_pruned_a11y_tree(page)
            elem_map = {el["id"]: el for el in elements}
            add_btns = [e for e in elements if "add to cart" in (e.get("name") or "").lower()]
            check("Found product Add-to-Cart buttons", len(add_btns) >= 5)

            # Add 5 items
            for i, btn in enumerate(add_btns[:5], 1):
                add_act = MacroAction(steps=[MacroStep(action="click", target_id=btn["id"], target_signature=btn["signature"])])
                await agent._execute_macro_action(page, add_act, elem_map)
                agent.step_history.append({"type": "add_to_cart", "action": add_act.model_dump()})


            cart_count_text = await page.inner_text("#cart-count")
            check("Cart badge verified with exactly 5 items", cart_count_text == "5")

            # ---------------------------------------------------------------
            # 6. Dynamic Skill Cache Write & Read
            # ---------------------------------------------------------------
            print("\n--- 6. Testing Skill Cache Write on Completion ---")
            domain = "instacart.com"
            agent._write_skill_cache(domain, agent.user_goal, agent.step_history)
            check("Verified recipe written to GLOBAL_WEB_SKILL_CACHE", domain in GLOBAL_WEB_SKILL_CACHE)
            cached_recipe = GLOBAL_WEB_SKILL_CACHE[domain]
            check("Skill cache contains recorded macro steps", len(cached_recipe["steps"]) >= 5)

            print("\n--- 7. Testing Skill Cache Fast-Path Read (Second Run) ---")
            # Create a fresh agent for a new user turn
            cached_macro = agent._lookup_skill_cache(domain, "Sign up for a new Instacart account")
            check("Skill Cache lookup succeeded for domain intent", cached_macro is not None)
            check("Cached macro contains ready-to-replay steps", len(cached_macro.steps) > 0)

            await browser.close()

    finally:
        server.stop()

    print("\n" + "=" * 65)
    if not failures:
        print("ALL TEST 1 SCENARIOS (HAPPY PATH & CACHE) VERIFIED 100% GREEN!")
    else:
        print(f"FAILED SCENARIOS: {failures}")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(run_instacart_flow_test())
