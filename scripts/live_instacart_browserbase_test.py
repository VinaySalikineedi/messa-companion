"""Live Instacart Test on Browserbase with Residential Proxies.

Runs run_light_web_task against live https://www.instacart.com in a real cloud
Browserbase session, producing a live session ID, inspector URL, and screenshot.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from messa import config
from messa.channels.browser import run_light_web_task
from messa.channels.browser_session_manager import session_manager

ARTIFACT_DIR = Path("/Users/robocafedesktop/.gemini/antigravity-ide/brain/0d960b0d-a3f3-42ec-8a1f-9bc67641b3d0")
TARGET_URL = "https://www.instacart.com"
GOAL = "Sign up for a new Instacart account and add 5 grocery items to the cart"


async def main():
    print("=" * 65)
    print("LIGHT-WEB-AGENT: LIVE INSTACART TEST (BROWSERBASE)")
    print("=" * 65)
    print(f"Goal:        {GOAL}")
    print(f"Target URL:  {TARGET_URL}")
    print(f"Provider:    {config.BROWSER_PROVIDER} (Residential: {config.BROWSERBASE_USE_RESIDENTIAL_PROXIES})")
    print(f"Model:       {config.LIGHT_WEB_AGENT_MODEL}")
    print("-" * 65)

    start_time = time.perf_counter()
    user_id = "live_instacart_shopper_test"

    credentials = {
        "instacart_email": "jane.shopper.messa@gmail.com",
        "instacart_password": "SecurePassword987!",
        "instacart_name": "Jane Shopper",
    }

    try:
        result = await run_light_web_task(
            user_id=user_id,
            goal=GOAL,
            initial_url=TARGET_URL,
            credentials=credentials,
            max_steps=5,
        )

        elapsed = round(time.perf_counter() - start_time, 2)
        print("\n" + "=" * 65)
        print("EXECUTION RESULT")
        print("=" * 65)
        print(f"Status:       {result.get('status')}")
        print(f"Session ID:   {result.get('session_id')}")
        print(f"Live View:    {result.get('live_view_url')}")
        print(f"Steps Taken:  {result.get('steps_taken')}")
        print(f"Total Time:   {elapsed}s")
        print(f"Summary:      {result.get('result_summary') or result.get('thought')}")
        if result.get("step_history"):
            print("\nStep History:")
            for i, st in enumerate(result["step_history"], 1):
                print(f"  Step {i} ({st.get('type')}): success={st.get('success', True)} err={st.get('error')}")
                if st.get("action"):
                    print(f"    Action: {st['action'].get('steps')}")
        if result.get("checkpoint"):
            print(f"Checkpoint:   {result.get('checkpoint')}")

        screenshot_path = ARTIFACT_DIR / "instacart_browserbase_live.png"
        if screenshot_path.exists():
            print(f"Screenshot:   Verified at {screenshot_path.name}")

    except Exception as e:
        print(f"\n[ERROR] Execution failed: {e}")
        import traceback
        traceback.print_exc()

    finally:
        print("\nCleaning up live session...")
        await session_manager.release_session(user_id)
        print("Session released successfully.")
        print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
