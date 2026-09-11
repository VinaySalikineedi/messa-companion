"""Unit and integration tests for dual-provider cloud browser architecture (Browserbase + Kernel).

Verifies:
1. Config flags & default values (BROWSER_PROVIDER, KERNEL_BASE_URL, BROWSERBASE_USE_RESIDENTIAL_PROXIES).
2. Browserbase residential proxy routing & CAPTCHA solver injection in create_session.
3. Kernel client auth headers, missing-key error handling, and stealth create_session mapping.
4. Unified browser router (messa.channels.browser) dynamic switching between Browserbase and Kernel.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

# Ensure required test dummy env vars are set
os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import config  # noqa: E402
from messa.channels import browser, browserbase, kernel  # noqa: E402
from messa.channels.kernel import KernelError  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def test_config_defaults() -> None:
    print("\n--- TEST 1: Config Defaults ---")
    check("BROWSER_PROVIDER defaults to 'browserbase'", config.BROWSER_PROVIDER == "browserbase")
    check("KERNEL_BASE_URL is 'https://api.kernel.sh/v1'", config.KERNEL_BASE_URL == "https://api.kernel.sh/v1")
    check("BROWSERBASE_USE_RESIDENTIAL_PROXIES is a bool (True)", config.BROWSERBASE_USE_RESIDENTIAL_PROXIES is True)


def test_browserbase_residential_proxies() -> None:
    print("\n--- TEST 2: Browserbase Residential Proxy & Captcha Injection ---")
    captured: dict[str, Any] = {}

    async def fake_bb_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {"id": "bb_sess_test", "connectUrl": "wss://browserbase.example/cdp"}

    real_req = browserbase._request
    browserbase._request = fake_bb_request
    try:
        asyncio.run(browserbase.create_session("ctx_test_123"))
    finally:
        browserbase._request = real_req

    body = captured.get("json") or {}
    settings = body.get("browserSettings") or {}
    proxies = body.get("proxies") or []

    check("create_session posts to /sessions", captured.get("path") == "/sessions")
    check("solveCaptchas is enabled in browserSettings", settings.get("solveCaptchas") is True)
    check("context is preserved", settings.get("context") == {"id": "ctx_test_123", "persist": True})
    check(
        "residential proxies configured with US geo",
        any(p.get("type") == "browserbase" and p.get("geolocation", {}).get("country") == "US" for p in proxies),
    )


def test_kernel_client() -> None:
    print("\n--- TEST 3: Kernel Client (kernel.py) ---")
    # 1. Missing key raises KernelError
    orig_key = config.KERNEL_API_KEY
    config.KERNEL_API_KEY = None
    try:
        threw = False
        try:
            kernel._headers()
        except KernelError:
            threw = True
        check("Missing KERNEL_API_KEY raises KernelError", threw)
    finally:
        config.KERNEL_API_KEY = orig_key

    # 2. Kernel headers with API key
    config.KERNEL_API_KEY = "test-kernel-key-xyz"
    headers = kernel._headers()
    check("Kernel Authorization header is Bearer", headers.get("Authorization") == "Bearer test-kernel-key-xyz")

    # 3. Kernel create_session with stealth and profile
    captured: dict[str, Any] = {}

    async def fake_kernel_request(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {
            "session_id": "kernel_sess_abc",
            "cdp_ws_url": "wss://kernel.example/cdp",
            "browser_live_view_url": "https://kernel.example/live/abc",
        }

    real_k_req = kernel._request
    kernel._request = fake_kernel_request
    try:
        res = asyncio.run(kernel.create_session("user_profile_42", stealth=True))
    finally:
        kernel._request = real_k_req

    body = captured.get("json") or {}
    check("Kernel create_session posts to /browsers", captured.get("path") == "/browsers")
    check("Kernel stealth mode is True", body.get("stealth") is True)
    check("Kernel profile name attached", body.get("profile", {}).get("name") == "user_profile_42")
    check("Kernel profile_save_changes is True", body.get("profile_save_changes") is True)
    check("Standardized id mapped from session_id", res.get("id") == "kernel_sess_abc")
    check("Standardized connectUrl mapped from cdp_ws_url", res.get("connectUrl") == "wss://kernel.example/cdp")
    check(
        "Standardized liveViewUrl mapped from browser_live_view_url",
        res.get("liveViewUrl") == "https://kernel.example/live/abc",
    )


def test_unified_browser_router() -> None:
    print("\n--- TEST 4: Unified Browser Router (browser.py) ---")
    orig_provider = config.BROWSER_PROVIDER
    try:
        # 1. Routing to Browserbase
        config.BROWSER_PROVIDER = "browserbase"
        check("get_active_provider returns 'browserbase'", browser.get_active_provider() == "browserbase")

        async def fake_bb_create(ctx_id: str | None = None) -> dict[str, Any]:
            return {"id": "bb_test_id", "connectUrl": "wss://bb.test/cdp"}

        real_bb_create = browserbase.create_session
        browserbase.create_session = fake_bb_create
        try:
            sess = asyncio.run(browser.create_session("ctx_99"))
            check("Router returns provider='browserbase'", sess.get("provider") == "browserbase")
            check("Router returns session ID from Browserbase", sess.get("id") == "bb_test_id")
        finally:
            browserbase.create_session = real_bb_create

        # 2. Routing to Kernel
        config.BROWSER_PROVIDER = "kernel"
        check("get_active_provider returns 'kernel'", browser.get_active_provider() == "kernel")

        async def fake_k_create(ctx_id: str | None = None, *, stealth: bool = True) -> dict[str, Any]:
            return {
                "id": "kernel_test_id",
                "connectUrl": "wss://kernel.test/cdp",
                "liveViewUrl": "https://kernel.test/live",
            }

        real_k_create = kernel.create_session
        kernel.create_session = fake_k_create
        try:
            sess = asyncio.run(browser.create_session(user_id=77))
            check("Router returns provider='kernel'", sess.get("provider") == "kernel")
            check("Router returns session ID from Kernel", sess.get("id") == "kernel_test_id")
            check("Router returns liveViewUrl from Kernel", sess.get("liveViewUrl") == "https://kernel.test/live")
        finally:
            kernel.create_session = real_k_create

    finally:
        config.BROWSER_PROVIDER = orig_provider


def main() -> None:
    test_config_defaults()
    test_browserbase_residential_proxies()
    test_kernel_client()
    test_unified_browser_router()

    print("\n" + "=" * 50)
    if failures:
        print(f"FAILED ({len(failures)} tests failed):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED! (4/4 test suites green)")
        sys.exit(0)


if __name__ == "__main__":
    main()
