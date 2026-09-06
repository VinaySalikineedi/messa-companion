"""Regression tests for the "Faster Than Human" branch's first round of
low-risk deepsearch upgrades (see faster-than-human.md) -- the ones that
don't depend on which browser-automation engine drives the page, so they
were built and tested against the EXISTING Playwright MCP + Browserbase
setup rather than waiting on a Stagehand decision:

  1. Ad/tracker blocking: Browserbase's native `browserSettings.blockAds`,
     wired into channels/browserbase.py's create_session().
  2. Cookie/consent banner auto-dismiss: assets/deepsearch_enhancements.js,
     injected via @playwright/mcp's --init-script flag (see
     test_fth_consent_and_form_js_live.py for the actual in-browser
     behavior -- this file only checks the Python-side wiring).
  3. Form-validation-error watcher: the new check_form_errors tool.
  4. Per-user concurrent-session cap, layered under the existing global cap.

No live Browserbase session or real network call is used here -- all
Browserbase/OpenRouter calls are monkeypatched or never reached, same
fake-pool-free/no-network style as test_privacy_page.py.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")

from messa import config, deepsearch_control  # noqa: E402
from messa.channels import browserbase  # noqa: E402
from messa.tools import deepsearch_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def part1_config_flags_exist_with_sane_defaults():
    check("DEEPSEARCH_BLOCK_ADS is a bool, default True", config.DEEPSEARCH_BLOCK_ADS is True)
    check("DEEPSEARCH_AUTO_DISMISS_CONSENT is a bool, default True", config.DEEPSEARCH_AUTO_DISMISS_CONSENT is True)
    check(
        "DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER is a positive int, layered under the global cap",
        isinstance(config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER, int)
        and 0 < config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER <= config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS,
    )


def part2_create_session_requests_blockAds():
    captured = {}

    async def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["json"] = kwargs.get("json")
        return {"id": "sess_fake", "connectUrl": "wss://example.com/fake"}

    real_request = browserbase._request
    browserbase._request = fake_request
    try:
        asyncio.run(browserbase.create_session(None))
    finally:
        browserbase._request = real_request

    body = captured.get("json") or {}
    settings = body.get("browserSettings", {})
    check("create_session POSTs to /sessions", captured.get("path") == "/sessions")
    check("browserSettings.blockAds is present and follows config.DEEPSEARCH_BLOCK_ADS",
          settings.get("blockAds") == config.DEEPSEARCH_BLOCK_ADS)
    check("viewport is still pinned (blockAds didn't clobber the existing field)",
          settings.get("viewport") == {"width": browserbase.VIEWPORT_WIDTH, "height": browserbase.VIEWPORT_HEIGHT})

    # A context_id should still thread through untouched alongside blockAds.
    captured.clear()
    browserbase._request = fake_request
    try:
        asyncio.run(browserbase.create_session("ctx_123"))
    finally:
        browserbase._request = real_request
    settings2 = (captured.get("json") or {}).get("browserSettings", {})
    check("context_id still passes through with blockAds also present",
          settings2.get("context") == {"id": "ctx_123", "persist": True} and "blockAds" in settings2)


def part3_check_form_errors_tool_registered():
    provider = deepsearch_tools.BrowserToolProvider()
    provider._build_tool_list([])  # no real MCP tool specs needed -- only checking the fixed appends
    tools_by_name = {t.name: t for t in provider.tools}

    check("check_form_errors is registered as a model-facing tool", "check_form_errors" in tools_by_name)
    check("await_email_verification_code is still registered alongside it",
          "await_email_verification_code" in tools_by_name)
    if "check_form_errors" in tools_by_name:
        desc = tools_by_name["check_form_errors"].description
        check("check_form_errors description explains NO_ERRORS_FOUND/FORM_ERRORS contract",
              "NO_ERRORS_FOUND" in desc and "FORM_ERRORS" in desc)


def part4_check_form_errors_parses_evaluate_result():
    provider = deepsearch_tools.BrowserToolProvider()

    class FakeEvaluateTool:
        def __init__(self, js_array):
            self._js_array = js_array

        async def coroutine(self, element=None, function=None, **kwargs):
            # Mirrors @playwright/mcp's real wrapping (see
            # deepsearch_tools.py's _EVALUATE_RESULT_RE comment): the JS
            # function's JSON.stringify(...) return value comes back
            # wrapped as "### Result\n<json-quoted-string>\n### Ran ...".
            inner = json.dumps(self._js_array)  # e.g. '["Email already exists"]'
            quoted = json.dumps(inner)  # JSON-quote it again, as Playwright's own result does
            return f"### Result\\n{quoted}\\n### Ran Playwright code\\n```js\\n// ...\\n```"

    async def _run_with_errors():
        provider._raw_tools_by_name = {"browser_evaluate": FakeEvaluateTool(["Email already exists"])}
        return await provider._check_form_errors()

    async def _run_with_no_errors():
        provider._raw_tools_by_name = {"browser_evaluate": FakeEvaluateTool([])}
        return await provider._check_form_errors()

    async def _run_with_no_evaluate_tool():
        provider._raw_tools_by_name = {}
        return await provider._check_form_errors()

    result_errors = asyncio.run(_run_with_errors())
    result_clean = asyncio.run(_run_with_no_errors())
    result_unavailable = asyncio.run(_run_with_no_evaluate_tool())

    check("a matched error is surfaced with the FORM_ERRORS: prefix and the actual text",
          result_errors.startswith("FORM_ERRORS:") and "Email already exists" in result_errors)
    check("an empty scan returns NO_ERRORS_FOUND:", result_clean.startswith("NO_ERRORS_FOUND:"))
    check("a missing browser_evaluate tool degrades to NO_ERRORS_FOUND: instead of raising",
          result_unavailable.startswith("NO_ERRORS_FOUND:"))


def part5_prompt_mentions_check_form_errors_before_escalation():
    prompt = deepsearch_tools.DEEPSEARCH_SYSTEM_PROMPT
    check("DEEPSEARCH_SYSTEM_PROMPT still contains the FORM SUBMISSION/EARLY ESCALATION rules",
          "FORM SUBMISSION" in prompt and "EARLY ESCALATION" in prompt)
    check("prompt tells the agent to call check_form_errors right after submitting",
          "check_form_errors" in prompt.split("FORM SUBMISSION")[1][:600])
    check("escalation rule references check_form_errors reporting the same error twice",
          "check_form_errors" in prompt.split("EARLY ESCALATION")[1][:200])


def part6_per_user_cap_uses_existing_active_count():
    # deepsearch_control.active_count already exists (built for "cancel my
    # active search") -- the per-user cap in build_deepsearch_subagent._run
    # is a pure read of it, so this exercises the exact primitive that check
    # is built on rather than the whole _run function (which needs a live
    # DB/model to construct meaningfully). register() keys off
    # asyncio.current_task(), so each simulated concurrent session has to
    # call register() from INSIDE its own task (matching how two of a
    # user's own texts each run their own top-level _run coroutine/task) --
    # not from the outer test coroutine, which would register the same task
    # repeatedly and never actually reach the cap.
    user_id = 999001

    async def _registered_session(uid: int) -> None:
        deepsearch_control.register(uid, "test session")
        await asyncio.sleep(3600)

    async def _scenario():
        check("no active sessions initially", deepsearch_control.active_count(user_id) == 0)
        tasks = [asyncio.create_task(_registered_session(user_id))
                 for _ in range(config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER)]
        await asyncio.sleep(0)  # let each task run up to its own register() call

        at_cap = deepsearch_control.active_count(user_id) >= config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER
        check(f"active_count reaches the per-user cap ({config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER}) "
              "after that many concurrent registrations", at_cap)

        # One MORE request from the same user, on top of the cap, is exactly
        # the case build_deepsearch_subagent._run's new early check declines
        # -- confirm the comparison it uses would actually trip here.
        would_decline = deepsearch_control.active_count(user_id) >= config.DEEPSEARCH_MAX_CONCURRENT_SESSIONS_PER_USER
        check("the >= comparison _run uses would decline one more request at the cap", would_decline)

        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
            deepsearch_control.unregister(user_id, t)
        check("active_count drops back to 0 once every session unregisters",
              deepsearch_control.active_count(user_id) == 0)

    asyncio.run(_scenario())


def main():
    part1_config_flags_exist_with_sane_defaults()
    part2_create_session_requests_blockAds()
    part3_check_form_errors_tool_registered()
    part4_check_form_errors_parses_evaluate_result()
    part5_prompt_mentions_check_form_errors_before_escalation()
    part6_per_user_cap_uses_existing_active_count()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
