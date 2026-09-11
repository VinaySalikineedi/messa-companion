"""Live side-by-side benchmark comparing Browserbase vs Kernel on Walmart.

Navigates both browsers to Walmart search ('organic whole milk'), checks for
bot-detection challenges (PerimeterX 'Press & Hold'), measures load time,
extracts product results, saves screenshots, and releases all sessions.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from playwright.async_api import async_playwright

from messa import config
from messa.channels import browserbase, kernel

ARTIFACT_DIR = Path("/Users/robocafedesktop/.gemini/antigravity-ide/brain/0d960b0d-a3f3-42ec-8a1f-9bc67641b3d0")
TARGET_URL = "https://www.walmart.com/search?q=organic+whole+milk"


async def test_provider(name: str, create_fn, screenshot_path: Path) -> dict[str, Any]:
    print(f"\n{'='*20} TESTING {name.upper()} {'='*20}")
    start_time = time.perf_counter()
    session = None
    session_id = None
    result = {
        "provider": name,
        "session_id": None,
        "live_view_url": None,
        "blocked_by_captcha": False,
        "items_found": 0,
        "page_title": "",
        "latency_seconds": 0.0,
        "error": None,
    }

    try:
        print(f"[{name}] Creating cloud browser session...")
        session = await create_fn()
        session_id = session.get("id")
        result["session_id"] = session_id
        result["live_view_url"] = session.get("liveViewUrl") or session.get("debuggerFullscreenUrl")
        cdp_url = session.get("connectUrl")

        print(f"[{name}] Session ID: {session_id}")
        print(f"[{name}] Live View: {result['live_view_url']}")
        print(f"[{name}] Connecting Playwright over CDP...")

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = context.pages[0] if context.pages else await context.new_page()

            print(f"[{name}] Navigating to {TARGET_URL}...")
            nav_start = time.perf_counter()
            try:
                await page.goto(TARGET_URL, timeout=45000, wait_until="domcontentloaded")
            except Exception as nav_e:
                print(f"[{name}] Page goto warning: {nav_e}")

            # Wait a few seconds for anti-bot or product grid to settle
            await page.wait_for_timeout(6000)

            title = await page.title()
            result["page_title"] = title
            print(f"[{name}] Page Title: '{title}'")

            content = await page.content()
            lowered = content.lower()

            # Anti-bot detection signatures
            if any(sig in lowered for sig in [
                "robot or human?", "press and hold", "press & hold", "px-captcha",
                "perimeterx", "blocked", "access denied", "verify you are human"
            ]):
                result["blocked_by_captcha"] = True
                print(f"[{name}] 🚨 BOT DETECTION TRIGGERED: 'Press & Hold' / PerimeterX detected!")
            else:
                print(f"[{name}] ✅ NO BOT WALL DETECTED!")

            # Count products or search items
            items = await page.query_selector_all("[data-testid='list-view'], [data-testid='item-stack'], [data-item-id]")
            result["items_found"] = len(items)
            print(f"[{name}] Product elements found: {len(items)}")

            # Save screenshot
            await page.screenshot(path=str(screenshot_path), full_page=False)
            print(f"[{name}] Screenshot saved to {screenshot_path.name}")

            await browser.close()

    except Exception as e:
        print(f"[{name}] Error: {e}")
        result["error"] = str(e)

    finally:
        result["latency_seconds"] = round(time.perf_counter() - start_time, 2)
        if session_id:
            print(f"[{name}] Releasing session {session_id}...")
            if name == "Kernel":
                await kernel.release_session(session_id)
            else:
                await browserbase.release_session(session_id)
            print(f"[{name}] Session released.")

    return result


async def main():
    print("Starting Live Comparison Benchmark: Browserbase vs Kernel on Walmart...")
    print(f"Target: {TARGET_URL}")

    # 1. Test Browserbase
    bb_screenshot = ARTIFACT_DIR / "walmart_browserbase_test.png"
    bb_result = await test_provider(
        "Browserbase",
        lambda: browserbase.create_session(None),
        bb_screenshot,
    )

    # 2. Test Kernel
    kernel_screenshot = ARTIFACT_DIR / "walmart_kernel_test.png"
    kernel_result = await test_provider(
        "Kernel",
        lambda: kernel.create_session(stealth=True),
        kernel_screenshot,
    )

    print("\n" + "=" * 50)
    print("BENCHMARK SUMMARY")
    print("=" * 50)
    for r in [bb_result, kernel_result]:
        status_icon = "❌ BLOCKED (Press & Hold)" if r["blocked_by_captcha"] else "✅ SUCCESS (Passed)"
        print(f"\nProvider: {r['provider']}")
        print(f"  Status:       {status_icon}")
        print(f"  Title:        {r['page_title']}")
        print(f"  Items Found:  {r['items_found']}")
        print(f"  Total Time:   {r['latency_seconds']}s")
        if r["error"]:
            print(f"  Error:        {r['error']}")


if __name__ == "__main__":
    asyncio.run(main())
