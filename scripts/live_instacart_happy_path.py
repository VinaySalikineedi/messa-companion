"""Live Instacart Happy-Path Test on Cloud Browserbase with Live View.

Runs the LightWebAgent against real https://www.instacart.com, printing
the Browserbase live view URL immediately, logging every cognitive step
(perception, landmark vs reasoning, macro-action batching, and human checkpoints),
and keeping the session alive for user observation.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from messa import config, db, live_activity
from messa.channels.browser import get_active_provider
from messa.channels.browser_session_manager import session_manager
from messa.channels.light_web_agent import LightWebAgent
from playwright.async_api import async_playwright

ARTIFACT_DIR = Path("/Users/robocafedesktop/.gemini/antigravity-ide/brain/0d960b0d-a3f3-42ec-8a1f-9bc67641b3d0")
TARGET_URL = "https://www.instacart.com"
GOAL = "Sign up for a new Instacart account and add 5 grocery items to the cart"


async def main():
    print("\n" + "=" * 75)
    print("LIGHT-WEB-AGENT: LIVE INSTACART CLOUD RUN (BROWSERBASE)")
    print("=" * 75)
    print(f"Goal:             {GOAL}")
    print(f"Target URL:       {TARGET_URL}")
    print(f"Browser Provider: {config.BROWSER_PROVIDER} (Residential Proxy: {config.BROWSERBASE_USE_RESIDENTIAL_PROXIES})")
    print(f"Cognitive Model:  {config.LIGHT_WEB_AGENT_MODEL}")
    print(f"Critic Model:     {config.LIGHT_WEB_AGENT_CRITIC_MODEL}")
    print("-" * 75)

    user_id = 101  # Integer user ID for Messa live_activity and active task scratchpad
    credentials = {
        "email": "jane.shopper.messa@gmail.com",
        "password": "SecurePassword987!",
        "name": "Jane Shopper",
        "instacart_email": "jane.shopper.messa@gmail.com",
        "instacart_password": "SecurePassword987!",
        "instacart_name": "Jane Shopper",
    }

    managed = None
    try:
        # 1. Create or get persistent session on Browserbase
        print("[1/5] Provisioning cloud browser on Browserbase...")
        managed = await session_manager.get_or_create_session(
            user_id=str(user_id),
            stealth=True,
        )

        session_id = managed.session_id
        live_view_url = managed.live_view_url or f"https://www.browserbase.com/sessions/{session_id}"
        dashboard_url = f"https://www.browserbase.com/sessions/{session_id}"

        # Initialize live activity for live view stream
        live_activity.start(user_id, GOAL)
        live_activity.set_session_id(user_id, session_id)

        print("\n" + "#" * 75)
        print(">>> BROWSERBASE LIVE SESSION READY <<<")
        print(f"Session ID:     {session_id}")
        print(f"Live View Link: {live_view_url}")
        print(f"Dashboard Link: {dashboard_url}")
        print("#" * 75 + "\n")

        # 2. Connect Playwright over CDP
        print("[2/5] Connecting Playwright client over remote CDP...")
        async with async_playwright() as p:
            page = await session_manager.connect_playwright(managed, p)

            # 3. Navigate to target
            print(f"[3/5] Navigating to {TARGET_URL} (domcontentloaded)...")
            await page.goto(TARGET_URL, timeout=45000, wait_until="domcontentloaded")
            await asyncio.sleep(2.0)

            # Save initial screenshot
            init_ss = ARTIFACT_DIR / "instacart_live_initial.png"
            try:
                await page.screenshot(path=str(init_ss))
                print(f"[Snapshot] Initial page saved to {init_ss.name}")
            except Exception as e:
                print(f"[Snapshot Warning] {e}")

            # 4. Instantiate LightWebAgent with user_id and run cognitive loop
            print(f"[4/5] Starting LightWebAgent autonomous cognitive loop (max 8 steps)...", flush=True)
            agent = LightWebAgent(
                session=managed,
                user_goal=GOAL,
                credentials=credentials,
                max_steps=8,
                user_id=user_id,
            )

            decision = await agent.run()

            print("\n" + "=" * 75, flush=True)
            print("AGENT RUN COMPLETED", flush=True)
            print("=" * 75, flush=True)
            print(f"Final Status:     {decision.status}", flush=True)
            print(f"Total Steps:      {len(agent.step_history)}", flush=True)
            print(f"Result Summary:   {decision.result_summary or decision.thought}", flush=True)
            if decision.checkpoint:
                print(f"Checkpoint Kind:  {decision.checkpoint.kind.upper()}", flush=True)
                print(f"Prompt to User:   {decision.checkpoint.prompt_to_user}", flush=True)
            print("-" * 75, flush=True)

            # Check live_activity status
            status_data = live_activity.get(user_id)
            print(f"[Live Activity Active URL]  {status_data.get('url')}", flush=True)
            print(f"[Live Activity Active Desc] {status_data.get('description')}", flush=True)
            print(f"[Live Activity Total Steps] {len(status_data.get('steps', []))}", flush=True)
            for s in status_data.get("steps", []):
                print(f"  -> {s}", flush=True)

            # Check scratchpad artifacts
            print("\n[Scratchpad Task Artifacts]:", flush=True)
            try:
                task = db.get_active_task(user_id)
                if task:
                    print(f"  Task #{task.get('id')} ({task.get('status')}): {task.get('artifacts')}", flush=True)
                else:
                    print(f"  In-memory task state: {agent.scratchpad_artifacts}", flush=True)
            except Exception as e:
                print(f"  In-memory task state: {agent.scratchpad_artifacts} (db err: {e})", flush=True)

            # Save post-execution screenshot
            final_ss = ARTIFACT_DIR / "instacart_live_final.png"
            try:
                await page.screenshot(path=str(final_ss))
                print(f"\n[Snapshot] Final page saved to {final_ss.name}")
            except Exception as e:
                print(f"[Snapshot Warning] {e}")

            # 5. Hold session open for user live view inspection
            HOLD_SECONDS = 90
            print("\n" + "=" * 75, flush=True)
            print(f"[5/5] HOLDING SESSION OPEN FOR {HOLD_SECONDS} SECONDS FOR LIVE INSPECTION", flush=True)
            print(f"You can watch/interact in DevTools Live View now:", flush=True)
            print(f"-> {live_view_url}", flush=True)
            print(f"Dashboard: {dashboard_url}", flush=True)
            print("=" * 75, flush=True)
            for remaining in range(HOLD_SECONDS, 0, -10):
                print(f"  Session holding open... {remaining}s remaining (pinging keepalive)...", flush=True)
                await managed.ping_keepalive()
                await asyncio.sleep(min(10, remaining))

    except Exception as e:
        print(f"\n[ERROR] Live test encountered an exception: {e}")
        import traceback
        traceback.print_exc()

    finally:
        live_activity.clear(user_id)
        if managed:
            print("\nReleasing Browserbase cloud session...")
            await session_manager.release_session(managed.session_id)
            print("Session cleanly released.")
            print("=" * 75 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
