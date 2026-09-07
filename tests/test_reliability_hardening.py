"""Tests for the deepsearch/integrations reliability-hardening pass
(docs/smart_autonomous_agent_architecture.md; plan:
/root/.claude/plans/glowing-forging-pumpkin.md):

  - stagehand_tools.py's `_wait_gate_active` flag -- the actual fix for the
    OTP-invalidation incident (close_browser refuses while
    await_email_verification_code is in flight).
  - tools/browser_circuit_breaker.py's StagehandZeroDeltaMiddleware -- the
    generalized "zero-delta" circuit breaker for the Stagehand engine.
  - tools/integration_circuit_breaker.py's IntegrationRetryLoopMiddleware --
    the equivalent breaker (plus save_skill nudge) for integrations_agent.
  - tools/integration_tools.py's new describe_integration_tool tool and
    execute_integration_tool's new auto-search-skills-on-failure path.
  - Structural wiring checks (registry.py's integrations_agent middleware,
    deepsearch_tools.py's engine-gated breaker attachment) -- same
    source-inspection convention test_scratchpad_and_skills.py's own
    part9_wiring_present already uses for wiring-only changes.

No live browser, no live Composio, no live LLM call -- same FakeConn/
FakeClient-free, plain-double convention as test_app_connect_queue.py and
test_scratchpad_and_skills.py (this file needs no DB pool at all, since
none of this logic touches asyncpg directly).
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
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

from langchain_core.messages import ToolMessage  # noqa: E402

from messa import config, db  # noqa: E402
from messa.approval import AutoApproveGate  # noqa: E402
from messa.tools import browser_circuit_breaker  # noqa: E402
from messa.tools import integration_circuit_breaker  # noqa: E402
from messa.tools import integration_tools as it  # noqa: E402
from messa.tools import stagehand_tools  # noqa: E402

failures = []


def check(label, cond):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


def _make_user(user_id=1, channel="sms", phone="+15551234567"):
    return config.UserContext(user_id=user_id, phone_number=phone, channel=channel)


# ---------------------------------------------------------------------------
# Part 1: the actual OTP-invalidation fix -- close_browser refuses while an
# email-verification wait is in flight.
# ---------------------------------------------------------------------------

class _FakeStagehandInstance:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


class _FakeBrowserInstance:
    def __init__(self):
        self.close_calls = 0

    async def close(self):
        self.close_calls += 1


async def part1_close_browser_blocked_during_otp_wait():
    fake_stagehand = _FakeStagehandInstance()
    fake_browser = _FakeBrowserInstance()

    provider = stagehand_tools.StagehandToolProvider(
        approval_gate=None, user_id=1, deepsearch_session_id=1,
        messa_email="user@messa.example",
        stagehand_instance=fake_stagehand, browser_instance=fake_browser,
    )

    async def fake_find_recent_otp(*a, **kw):
        return None  # never finds a code -- keeps the wait loop running

    async def fake_clear_live(*a, **kw):
        pass

    db.find_recent_otp_in_inbox = fake_find_recent_otp
    db.clear_live_browser_active = fake_clear_live

    check("wait gate starts inactive", provider._wait_gate_active is False)

    wait_task = asyncio.create_task(provider._await_email_verification_code())
    await asyncio.sleep(0)  # let the coroutine run up to its first await
    check("wait gate is active once the OTP wait has started", provider._wait_gate_active is True)

    close_result = await provider._close_browser()
    check("close_browser refuses while the OTP wait is in flight", "BLOCKED" in close_result)
    check("stagehand.close() was never called while blocked", fake_stagehand.close_calls == 0)
    check("browser.close() was never called while blocked", fake_browser.close_calls == 0)
    check("the browser objects are still set (nothing was torn down)", provider.stagehand is not None and provider.browser is not None)

    wait_task.cancel()
    try:
        await wait_task
    except asyncio.CancelledError:
        pass
    check("wait gate clears once the wait ends", provider._wait_gate_active is False)

    close_result2 = await provider._close_browser()
    check("close_browser proceeds normally once the wait is no longer active", "closed successfully" in close_result2)
    check("stagehand.close() was called once the gate cleared", fake_stagehand.close_calls == 1)
    check("browser.close() was called once the gate cleared", fake_browser.close_calls == 1)


def part1_request_human_help_shares_the_same_gate():
    """request_human_help is the other blocking poll loop (2FA/CAPTCHA
    hand-off) that can race close_browser the same way an OTP wait can --
    confirmed via source inspection that it sets/clears the identical
    `self._wait_gate_active` flag, rather than re-driving its full flow
    here (which needs a real page/get_active_page setup close_browser's
    own guard doesn't depend on)."""
    src = (REPO_ROOT / "messa" / "tools" / "stagehand_tools.py").read_text()
    request_help_start = src.index("async def _request_human_help")
    request_help_body = src[request_help_start:src.index("async def _await_email_verification_code")]
    check(
        "_request_human_help sets self._wait_gate_active = True",
        "self._wait_gate_active = True" in request_help_body,
    )
    check(
        "_request_human_help clears self._wait_gate_active = False in a finally block",
        "self._wait_gate_active = False" in request_help_body,
    )


# ---------------------------------------------------------------------------
# Part 2: StagehandZeroDeltaMiddleware -- the generalized breaker for the
# Stagehand engine.
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self, name, args=None, call_id="c1"):
        self.tool_call = {"name": name, "args": args or {}, "id": call_id}


class _FakeStagehandPage:
    def __init__(self, url="https://example.com/start", title="Start Page"):
        self.url = url
        self._title = title

    async def title(self):
        return self._title


class _FakeStagehandProvider:
    def __init__(self, page):
        self._page = page

    async def get_active_page(self):
        return self._page

    async def _get_url(self, page):
        return page.url


async def part2_zero_delta_breaker_trips_on_repeats():
    page = _FakeStagehandPage()
    provider = _FakeStagehandProvider(page)
    mw = browser_circuit_breaker.StagehandZeroDeltaMiddleware(provider, max_zero_delta_repeats=3)

    async def noop_handler(request):
        return ToolMessage(content="Action Result: nothing happened", tool_call_id=request.tool_call["id"])

    req = _FakeRequest("browser_act", {"instruction": "click the disabled button"})

    r1 = await mw.awrap_tool_call(req, noop_handler)
    check("a zero-delta call below the threshold returns normally", isinstance(r1, ToolMessage))
    await mw.awrap_tool_call(req, noop_handler)  # 2nd identical -- still under threshold (3)

    tripped = False
    try:
        await mw.awrap_tool_call(req, noop_handler)
    except browser_circuit_breaker.ZeroDeltaExceeded:
        tripped = True
    check("the breaker trips on the Nth consecutive zero-delta call (max_zero_delta_repeats=3)", tripped)


async def part2_zero_delta_breaker_resets_on_real_progress():
    page = _FakeStagehandPage()
    provider = _FakeStagehandProvider(page)
    mw = browser_circuit_breaker.StagehandZeroDeltaMiddleware(provider, max_zero_delta_repeats=3)

    async def noop_handler(request):
        return ToolMessage(content="Action Result: nothing happened", tool_call_id=request.tool_call["id"])

    async def progressing_handler(request):
        page.url = "https://example.com/next-step"
        page._title = "Next Step"
        return ToolMessage(content="Action Result: advanced", tool_call_id=request.tool_call["id"])

    req = _FakeRequest("browser_act", {"instruction": "do something"})
    await mw.awrap_tool_call(req, noop_handler)
    await mw.awrap_tool_call(req, noop_handler)  # streak = 2, one away from tripping at 3
    await mw.awrap_tool_call(req, progressing_handler)  # real change -- resets the streak
    # Two more zero-delta calls after the reset should NOT trip (streak restarts at 0->2, not 2->4).
    await mw.awrap_tool_call(req, noop_handler)
    tripped = False
    try:
        await mw.awrap_tool_call(req, noop_handler)
    except browser_circuit_breaker.ZeroDeltaExceeded:
        tripped = True
    check("a fingerprint-changing call resets the zero-delta streak instead of accumulating", not tripped)


async def part2_zero_delta_breaker_ignores_unrelated_tools():
    page = _FakeStagehandPage()
    provider = _FakeStagehandProvider(page)
    mw = browser_circuit_breaker.StagehandZeroDeltaMiddleware(provider, max_zero_delta_repeats=2)

    async def noop_handler(request):
        return ToolMessage(content="Extracted Content: nothing new", tool_call_id=request.tool_call["id"])

    req = _FakeRequest("browser_extract", {"instruction": "get the price"})
    tripped = False
    try:
        for _ in range(10):
            await mw.awrap_tool_call(req, noop_handler)
    except browser_circuit_breaker.ZeroDeltaExceeded:
        tripped = True
    check("a tool outside the fingerprinted set (browser_extract) is never breaker-tripped", not tripped)
    check(
        "browser_execute_script IS included in the fingerprinted set (same failure class as browser_act)",
        "browser_execute_script" in browser_circuit_breaker.FINGERPRINTED_TOOL_NAMES,
    )


def part2_breaker_only_attached_for_stagehand_engine():
    src = (REPO_ROOT / "messa" / "tools" / "deepsearch_tools.py").read_text()
    check(
        "deepsearch_tools.py imports StagehandZeroDeltaMiddleware",
        "from .browser_circuit_breaker import StagehandZeroDeltaMiddleware" in src,
    )
    check(
        "the breaker is only appended when the Stagehand engine is selected",
        "if _use_stagehand:" in src and "_run_middleware.append(StagehandZeroDeltaMiddleware(provider))" in src,
    )


# ---------------------------------------------------------------------------
# Part 3: IntegrationRetryLoopMiddleware -- the equivalent breaker (plus
# save_skill nudge) for integrations_agent.
# ---------------------------------------------------------------------------

async def part3_integration_retry_loop_and_nudge():
    mw = integration_circuit_breaker.IntegrationRetryLoopMiddleware(max_identical_attempts=2)
    handler_calls = []

    async def handler(request):
        handler_calls.append(dict(request.tool_call["args"]))
        slug = request.tool_call["args"]["slug"]
        args = request.tool_call["args"]["arguments"]
        if args.get("mode") == "fail":
            return ToolMessage(content=f"'{slug}' failed: boom", tool_call_id=request.tool_call["id"])
        return ToolMessage(content=f"Success for {slug}", tool_call_id=request.tool_call["id"])

    slug = "TODOIST_CREATE_TASK"
    fail_req = _FakeRequest("execute_integration_tool", {"slug": slug, "arguments": {"mode": "fail"}})

    await mw.awrap_tool_call(fail_req, handler)
    check("1st identical failure reaches the handler", len(handler_calls) == 1)
    await mw.awrap_tool_call(fail_req, handler)
    check("2nd identical failure (at max_identical_attempts) still reaches the handler", len(handler_calls) == 2)
    r3 = await mw.awrap_tool_call(fail_req, handler)
    check("3rd identical call is short-circuited before reaching the handler", len(handler_calls) == 2)
    check("the short-circuit result tells the model not to retry identically", "BLOCKED" in r3.content)

    diff_args_req = _FakeRequest("execute_integration_tool", {"slug": slug, "arguments": {"mode": "fail", "extra": 1}})
    await mw.awrap_tool_call(diff_args_req, handler)
    check("different arguments for the same slug are never short-circuited", len(handler_calls) == 3)

    success_req = _FakeRequest("execute_integration_tool", {"slug": slug, "arguments": {"mode": "ok"}})
    r5 = await mw.awrap_tool_call(success_req, handler)
    check("a success reaches the handler normally", len(handler_calls) == 4)
    check(
        "a success for a slug that failed earlier in this task gets a save_skill nudge appended",
        "save_skill" in r5.content,
    )

    other_req = _FakeRequest("execute_integration_tool", {"slug": "SLACK_POST_MESSAGE", "arguments": {"mode": "ok"}})
    r6 = await mw.awrap_tool_call(other_req, handler)
    check("a slug that never failed in this task gets no nudge on success", "save_skill" not in r6.content)


async def part3_retry_loop_ignores_unrelated_tools():
    mw = integration_circuit_breaker.IntegrationRetryLoopMiddleware(max_identical_attempts=1)

    async def handler(request):
        return ToolMessage(content="fine", tool_call_id=request.tool_call["id"])

    req = _FakeRequest("search_integration_tools", {"query": "todoist"})
    for _ in range(5):
        result = await mw.awrap_tool_call(req, handler)
    check("a non-execute_integration_tool call is always passed straight through", result.content == "fine")


def part3_registry_wiring():
    src = (REPO_ROOT / "messa" / "agents" / "registry.py").read_text()
    check("registry.py imports ModelCallLimitMiddleware", "from langchain.agents.middleware import ModelCallLimitMiddleware" in src)
    check(
        "registry.py imports IntegrationRetryLoopMiddleware",
        "from ..tools.integration_circuit_breaker import IntegrationRetryLoopMiddleware" in src,
    )
    integrations_start = src.index('"name": "integrations_agent"')
    integrations_block = src[integrations_start:integrations_start + 2500]
    check(
        "integrations_agent's dict carries a real ModelCallLimitMiddleware step budget",
        "ModelCallLimitMiddleware(" in integrations_block
        and "run_limit=config.INTEGRATIONS_AGENT_MAX_MODEL_CALLS" in integrations_block,
    )
    check(
        "integrations_agent's dict carries IntegrationRetryLoopMiddleware",
        "IntegrationRetryLoopMiddleware()" in integrations_block,
    )


# ---------------------------------------------------------------------------
# Part 4: self-healing integrations -- describe_integration_tool + auto
# search-skills-on-failure inside execute_integration_tool.
# ---------------------------------------------------------------------------

class _FakeComposioToolObj:
    def __init__(self, description, input_parameters):
        self.description = description
        self.input_parameters = input_parameters


class _FakeToolsResource:
    def __init__(self):
        self.execute_calls = []
        self.raise_on_execute = None
        self.schema_by_slug = {}

    def execute(self, *, slug, arguments, user_id, **kw):
        self.execute_calls.append((slug, arguments))
        if self.raise_on_execute is not None:
            raise self.raise_on_execute
        return {"ok": True, "slug": slug}

    def get_raw_composio_tool_by_slug(self, slug):
        if slug not in self.schema_by_slug:
            raise KeyError(f"no such tool {slug!r}")
        return self.schema_by_slug[slug]


class _FakeComposioClient:
    def __init__(self):
        self.tools = _FakeToolsResource()


async def part4_describe_integration_tool():
    client = _FakeComposioClient()
    client.tools.schema_by_slug["TODOIST_CREATE_TASK"] = _FakeComposioToolObj(
        description="Create a Todoist task",
        input_parameters={"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]},
    )
    it._get_client = lambda: client
    user = _make_user()
    tools = it.build_integration_tools(user, AutoApproveGate())
    describe_tool = next(t for t in tools if t.name == "describe_integration_tool")

    result = await describe_tool.coroutine(slug="TODOIST_CREATE_TASK")
    check("describe_integration_tool returns the real description", "Create a Todoist task" in result)
    check("describe_integration_tool returns the real JSON parameter schema", '"content"' in result and '"required"' in result)

    result2 = await describe_tool.coroutine(slug="NOPE_SLUG")
    check("an unknown slug returns a clean error string, never a crash", "Couldn't look up" in result2)


async def part4_execute_integration_tool_self_healing():
    client = _FakeComposioClient()
    client.tools.raise_on_execute = RuntimeError("INVALID_ARGUMENT: missing spreadsheet_id")
    it._get_client = lambda: client
    user = _make_user()

    hits = [{"solution_recipe": "Always pass spreadsheet_id explicitly, never a title lookup."}]

    async def fake_search_skills(agent_type, domain, limit=None):
        check("the auto skills lookup is scoped to integrations_agent", agent_type == "integrations_agent")
        check("the auto skills lookup uses the guessed toolkit domain", domain == "todoist")
        return hits

    db.search_skills = fake_search_skills
    config.SCRATCHPAD_AND_SKILLS_ENABLED = True

    tools = it.build_integration_tools(user, AutoApproveGate())
    execute_tool = next(t for t in tools if t.name == "execute_integration_tool")
    result = await execute_tool.coroutine(slug="TODOIST_CREATE_TASK", arguments={"content": "x"})
    check("a failure still surfaces the underlying error", "failed" in result and "INVALID_ARGUMENT" in result)
    check(
        "a failure auto-includes a previously learned skill without the model calling search_skills itself",
        "Always pass spreadsheet_id explicitly" in result,
    )

    async def broken_search_skills(agent_type, domain, limit=None):
        raise RuntimeError("db is down")

    db.search_skills = broken_search_skills
    result2 = await execute_tool.coroutine(slug="TODOIST_CREATE_TASK", arguments={"content": "x"})
    check(
        "a broken skills lookup never hides or crashes the real underlying error",
        "failed" in result2 and "INVALID_ARGUMENT" in result2,
    )

    # Kill switch: with scratchpad+skills disabled, no skills lookup is attempted at all.
    lookup_calls = []

    async def poison_search_skills(agent_type, domain, limit=None):
        lookup_calls.append(True)
        raise AssertionError("search_skills must not be called while the feature flag is off")

    db.search_skills = poison_search_skills
    orig_flag = config.SCRATCHPAD_AND_SKILLS_ENABLED
    try:
        config.SCRATCHPAD_AND_SKILLS_ENABLED = False
        result3 = await execute_tool.coroutine(slug="TODOIST_CREATE_TASK", arguments={"content": "x"})
        check("the failure error still returns cleanly with the flag off", "failed" in result3)
        check("no skills lookup is attempted while the feature flag is off", len(lookup_calls) == 0)
    finally:
        config.SCRATCHPAD_AND_SKILLS_ENABLED = orig_flag


async def main() -> None:
    await part1_close_browser_blocked_during_otp_wait()
    part1_request_human_help_shares_the_same_gate()
    await part2_zero_delta_breaker_trips_on_repeats()
    await part2_zero_delta_breaker_resets_on_real_progress()
    await part2_zero_delta_breaker_ignores_unrelated_tools()
    part2_breaker_only_attached_for_stagehand_engine()
    await part3_integration_retry_loop_and_nudge()
    await part3_retry_loop_ignores_unrelated_tools()
    part3_registry_wiring()
    await part4_describe_integration_tool()
    await part4_execute_integration_tool_self_healing()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
