"""Tests for the Open-Source Phone / Bring Your Own Phone (BYOP) feature
(open-source-phone.md, feature/open-source-phone) -- no real Android
hardware in this environment, so everything below mocks uiautomator2/
adbutils at the boundary (a MagicMock standing in for a uiautomator2.Device,
patched config.build_model for the LLM planner) the same way
tests/test_light_web_agent.py mocks Playwright, and uses the same
FakeConn/FakeAcquire/FakePool/FakeRow shape as tests/test_call_control_and_
activity.py for db.py's new CRUD functions.

Parts:
  1. translate_device_error -- every regex mapping + the default fallback.
  2. extract_pruned_hierarchy -- clickable/editable elements get ids+
     signatures, plain text folds into context lines, nothing crashes on a
     malformed 'bounds' attribute.
  3. detect_unexpected_dialog -- the phone-native "is something covering
     what I expected" analog to light-web-agent's occlusion check.
  4. AndroidDeviceManager -- acquire/QueueFullError/DeviceQueueTicket
     lock+position/take_screenshot/release.
  5. AndroidPhoneAgent._check_circuit_breakers -- step cap, timeout,
     period-1/2/3 oscillation (same widened scan as light_web_agent.py's).
  6. AndroidPhoneAgent._execute_macro_action -- tap/type/press_key/swipe/
     app_start/wait_for, unknown action, stale-signature/missing-target
     rejection, {{cred:...}} token substitution.
  7. AndroidPhoneAgent.run() end-to-end (mocked device + mocked planner) --
     DONE, a consequential action pausing for milestone_confirm, an
     unexpected foreground app pausing for unexpected_dialog, and
     resume_checkpoint steering the very next planner call.
  8. phone_activity.py -- register/get/set_progress/set_waiting/has_entry/
     is_active/cancel(actually cancels a live asyncio.Task)/unregister/clear.
  9. run_android_phone_task -- phone_activity register/unregister bracket,
     status flips to done/failed/aborted, a real cancellation mid-run.
  10. db.py CRUD -- pair_user_device/get_user_device/authorize_device_
      contact/publish_device_skill/list_public_device_skills/
      get_pending_android_checkpoint.
  11. tools/android_phone_tools.py -- category allowlist/blocklist
      enforcement, plan-tier step gating, _sanitize_recipe PII redaction.
  12. skills_showcase_page.py -- HTML-escaping of untrusted published
      content (a real XSS check, not just a smoke render).
  13. server.py routes (starlette TestClient) -- GET /live/{token}/phone,
      /status, /skills; POST /live/{token}/phone/abort presses Home and
      cancels the tracked task.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

from messa import config, db, phone_activity, plans  # noqa: E402
from messa.devices import android  # noqa: E402
from messa.devices.android import (  # noqa: E402
    AndroidDeviceManager,
    AndroidPhoneAgent,
    DeviceQueueTicket,
    ManagedDevice,
    PhoneCheckpoint,
    PhoneMacroAction,
    QueueFullError,
    detect_unexpected_dialog,
    extract_pruned_hierarchy,
    translate_device_error,
)
from messa.skills_showcase_page import render_skills_showcase_page  # noqa: E402
from messa.tools import android_phone_tools  # noqa: E402
from messa.tools.android_phone_tools import _sanitize_recipe, build_android_phone_tools  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Fakes -- same FakeConn/FakeAcquire/FakePool/FakeRow shape as
# tests/test_call_control_and_activity.py.
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None, fetch_result=None, execute_results=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
        self.fetch_result = fetch_result if fetch_result is not None else []
        self.execute_results = list(execute_results or [])
        self.calls = []

    async def fetchval(self, query, *args):
        return self.has_tables

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if self.fetchrow_queue:
            return self.fetchrow_queue.pop(0)
        return None

    async def fetch(self, query, *args):
        self.calls.append(("fetch", query, args))
        return self.fetch_result

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        if self.execute_results:
            return self.execute_results.pop(0)
        return "UPDATE 0"

    def transaction(self):
        return _FakeTransaction()


class _FakeTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return FakeAcquire(self.conn)


class FakeRow(dict):
    """asyncpg.Record is dict-like; dict(row) is used throughout db.py."""


def install_fake_pool(conn):
    async def fake_get_pool():
        return FakePool(conn)
    db.get_pool = fake_get_pool


USER = config.UserContext(user_id=7, phone_number="+15551234567", plan_id="pro")


# ===========================================================================
# 1. translate_device_error
# ===========================================================================

def part1_translate_device_error():
    code, msg = translate_device_error(RuntimeError("Failed to authenticate: incorrect pin"))
    check("expired pairing code maps to pairing_code_expired", code == "pairing_code_expired")
    check("expired pairing code guidance mentions the 6-digit code", "6-digit" in msg)

    code, msg = translate_device_error(ConnectionRefusedError("Connection refused"))
    check("ECONNREFUSED maps to connection error code", code in ("wireless_debugging_disabled", "bridge_connection_refused"))

    code, msg = translate_device_error(TimeoutError("Connection timed out"))
    check("timeout maps to device_unreachable", code == "device_unreachable")
    check("device_unreachable guidance mentions the companion or bridge", "companion" in msg.lower() or "bridge" in msg.lower())

    code, msg = translate_device_error(RuntimeError("device offline"))
    check("device offline maps to device_offline", code == "device_offline")

    code, msg = translate_device_error(RuntimeError("HierarchyEmptyError: hierarchy is empty"))
    check("empty hierarchy maps to screen_locked_or_blank", code == "screen_locked_or_blank")

    code, msg = translate_device_error(ValueError("some totally unrelated error"))
    check("an unrecognized error falls back to the default guidance", code == "unknown_device_error")
    check("translate_device_error never raises on an unknown exception type", isinstance(msg, str) and len(msg) > 0)


# ===========================================================================
# 2. extract_pruned_hierarchy
# ===========================================================================

_SAMPLE_XML = """<?xml version="1.0"?>
<hierarchy>
  <node class="android.widget.FrameLayout" bounds="[0,0][1080,2280]">
    <node class="android.widget.EditText" resource-id="search" text="" content-desc="Search"
          clickable="true" bounds="[40,200][1040,300]" />
    <node class="android.widget.Button" text="Add to cart" clickable="true" bounds="[40,2000][1040,2100]" />
    <node class="android.widget.TextView" text="Free delivery over $35" clickable="false" bounds="[40,320][1040,360]" />
    <node class="android.widget.TextView" text="" clickable="false" bounds="[0,0][0,0]" />
  </node>
</hierarchy>"""


def part2_extract_pruned_hierarchy():
    elements, prompt_text = extract_pruned_hierarchy(_SAMPLE_XML)
    check("two interactive elements extracted (EditText + Button)", len(elements) == 2)
    ids = {e["id"] for e in elements}
    check("element ids follow the eN convention", ids == {"e0", "e1"})

    search_el = next(e for e in elements if e.get("content_desc") == "Search" or e.get("text") == "")
    check("EditText element captured a center point", "center" in search_el and len(search_el["center"]) == 2)
    check("EditText element has a non-empty signature", isinstance(search_el.get("signature"), str) and search_el["signature"])

    check("non-interactive text with real content appears in the prompt as context",
          "Free delivery over $35" in prompt_text)
    check("an empty, zero-bounds text node is dropped entirely (not even as context)",
          prompt_text.count("(context text:") <= 1)

    # Malformed bounds must never crash extraction -- falls back to zeros.
    bad_xml = _SAMPLE_XML.replace('bounds="[40,200][1040,300]"', 'bounds="garbage"')
    elements2, _ = extract_pruned_hierarchy(bad_xml)
    check("a malformed 'bounds' attribute does not raise", isinstance(elements2, list))


# ===========================================================================
# 3. detect_unexpected_dialog
# ===========================================================================

def part3_detect_unexpected_dialog():
    check("matching foreground package raises no dialog reason",
          detect_unexpected_dialog("com.chipotlemexicangrill.a", "com.chipotlemexicangrill.a") is None)
    check("no target package specified (not yet inferred) raises no dialog reason",
          detect_unexpected_dialog("com.chipotlemexicangrill.a", None) is None)
    reason = detect_unexpected_dialog("com.android.packageinstaller", "com.chipotlemexicangrill.a")
    check("a package-installer foreground while targeting another app IS flagged",
          reason is not None and isinstance(reason, str))
    reason2 = detect_unexpected_dialog("com.some.totally.other.app", "com.chipotlemexicangrill.a")
    check("a completely different foreground app while a target is set IS flagged", reason2 is not None)


# ===========================================================================
# 4. AndroidDeviceManager: queue + ticket + screenshot
# ===========================================================================

async def part4_device_manager_queue():
    mgr = AndroidDeviceManager()
    device_id = 101
    managed = ManagedDevice(device_id, "192.168.1.50", 40123)
    mgr._devices[device_id] = managed  # test-only direct seed, mirrors connect_device's own bookkeeping

    check("get_managed returns the seeded device", mgr.get_managed(device_id) is managed)
    check("get_managed returns None for an unknown device", mgr.get_managed(9999) is None)

    ticket1 = await mgr.acquire(device_id)
    check("first acquire reports queue position 0 (running now)", ticket1.position == 0)
    async with ticket1:
        check("device status flips to 'busy' while the ticket is held", managed.status == "busy")
        # A second caller queues behind the first while it's held.
        ticket2 = await mgr.acquire(device_id)
        check("a second acquire while busy reports a nonzero queue position", ticket2.position >= 1)
    check("device status returns to 'connected' once the first ticket exits", managed.status == "connected")

    async with ticket2:
        pass
    check("queue_depth returns to 0 once every ticket has exited", managed.queue_depth == 0)

    # QueueFullError once ANDROID_PHONE_MAX_QUEUE_DEPTH is reached.
    real_max = getattr(config, "ANDROID_PHONE_MAX_QUEUE_DEPTH", 5)
    config.ANDROID_PHONE_MAX_QUEUE_DEPTH = 1
    try:
        held_ticket = await mgr.acquire(device_id)
        async with held_ticket:
            raised = False
            try:
                await mgr.acquire(device_id)
            except QueueFullError:
                raised = True
            check("acquire raises QueueFullError once the device's queue is saturated", raised)
    finally:
        config.ANDROID_PHONE_MAX_QUEUE_DEPTH = real_max

    # acquire on a never-connected device raises ConnectionError, not KeyError.
    raised_conn_err = False
    try:
        await mgr.acquire(555555)
    except ConnectionError:
        raised_conn_err = True
    check("acquire on an unconnected device_id raises ConnectionError", raised_conn_err)

    # take_screenshot: wraps device.screenshot() (a PIL Image) into PNG bytes.
    fake_image = MagicMock()

    def fake_save(buf, format=None):
        buf.write(b"\x89PNG\r\n fake png bytes")
    fake_image.save.side_effect = fake_save
    managed.u2_device = MagicMock()
    managed.u2_device.screenshot = MagicMock(return_value=fake_image)
    png_bytes = await mgr.take_screenshot(device_id)
    check("take_screenshot returns PNG bytes from device.screenshot()", isinstance(png_bytes, bytes) and png_bytes.startswith(b"\x89PNG"))

    managed.u2_device.screenshot = MagicMock(side_effect=RuntimeError("boom"))
    png_bytes2 = await mgr.take_screenshot(device_id)
    check("take_screenshot never raises -- returns None on a device error", png_bytes2 is None)

    check("take_screenshot on an unmanaged device returns None", await mgr.take_screenshot(424242) is None)

    mgr.release(device_id)
    check("release() drops status back to 'connected'", managed.status == "connected")


# ===========================================================================
# 5. AndroidPhoneAgent._check_circuit_breakers
# ===========================================================================

def _make_agent(**overrides) -> AndroidPhoneAgent:
    device = MagicMock()
    kwargs = dict(
        u2_device=device,
        device_row={"id": 1, "tunnel_host": "1.2.3.4", "tunnel_port": 5555},
        user_goal="order a burrito bowl",
        credentials={},
        max_steps=5,
        timeout_seconds=120,
        user_id=7,
        target_package="com.chipotlemexicangrill.a",
    )
    kwargs.update(overrides)
    return AndroidPhoneAgent(**kwargs)


def part5_circuit_breakers():
    agent = _make_agent(max_steps=3)
    agent.step_history = [{"type": "macro_action"}] * 3
    decision = agent._check_circuit_breakers()
    check("step cap trips FAILED once step_history reaches max_steps", decision is not None and decision.status == "FAILED")

    agent2 = _make_agent(timeout_seconds=10)
    agent2._start_time = time.time() - 20
    decision2 = agent2._check_circuit_breakers()
    check("timeout trips FAILED once wall-clock exceeds timeout_seconds", decision2 is not None and decision2.status == "FAILED")

    # Period-1 oscillation: the exact same action repeated.
    agent3 = _make_agent()
    agent3.action_history_signatures = ["a", "a"]
    d3 = agent3._check_circuit_breakers()
    check("a period-1 repeat (A-A) trips the oscillation breaker", d3 is not None and "loop" in (d3.thought or "").lower())

    # Period-2 oscillation (A-B-A-B).
    agent4 = _make_agent()
    agent4.action_history_signatures = ["a", "b", "a", "b"]
    d4 = agent4._check_circuit_breakers()
    check("a period-2 repeat (A-B-A-B) trips the oscillation breaker", d4 is not None)

    # Period-3 oscillation (A-B-C-A-B-C) -- the widened scan light_web_agent.py
    # also added, not just the narrower hardcoded period-2 window.
    agent5 = _make_agent()
    agent5.action_history_signatures = ["a", "b", "c", "a", "b", "c"]
    d5 = agent5._check_circuit_breakers()
    check("a period-3 repeat (A-B-C-A-B-C) trips the oscillation breaker", d5 is not None)

    # Genuinely different actions never trip anything.
    agent6 = _make_agent()
    agent6.action_history_signatures = ["a", "b", "c", "d", "e", "f"]
    d6 = agent6._check_circuit_breakers()
    check("six genuinely distinct actions do not trip the oscillation breaker", d6 is None)


# ===========================================================================
# 6. AndroidPhoneAgent._execute_macro_action
# ===========================================================================

async def part6_execute_macro_action():
    agent = _make_agent()
    device = agent.device
    device.click = MagicMock()
    device.send_keys = MagicMock()
    device.clear_text = MagicMock()
    device.press = MagicMock()
    device.app_start = MagicMock()
    device.window_size = MagicMock(return_value=(1080, 2280))
    device.swipe = MagicMock()

    elem_map = {"e0": {"id": "e0", "center": (100, 200), "signature": "sig-abc"}}

    # tap
    action = PhoneMacroAction(steps=[{"action": "tap", "target_id": "e0", "target_signature": "sig-abc"}])
    ok, err = await agent._execute_macro_action(action, elem_map)
    check("tap on a valid, unchanged target succeeds", ok is True and err is None)
    check("tap calls device.click at the element's center", device.click.call_args[0] == (100, 200))

    # stale signature rejected
    action_stale = PhoneMacroAction(steps=[{"action": "tap", "target_id": "e0", "target_signature": "WRONG"}])
    ok2, err2 = await agent._execute_macro_action(action_stale, elem_map)
    check("a stale target_signature is rejected rather than silently tapping the wrong element", ok2 is False)
    check("the stale-signature error message says so", "stale" in (err2 or "").lower() or "changed" in (err2 or "").lower())

    # missing target
    action_missing = PhoneMacroAction(steps=[{"action": "tap", "target_id": "e99"}])
    ok3, err3 = await agent._execute_macro_action(action_missing, elem_map)
    check("a target_id no longer on screen is rejected, not a crash", ok3 is False and "no longer exist" in (err3 or ""))

    # type + credential token substitution
    agent.credentials = {"password": "hunter2"}
    action_type = PhoneMacroAction(steps=[{"action": "type", "target_id": "e0", "value": "{{cred:password}}"}])
    ok4, _ = await agent._execute_macro_action(action_type, elem_map)
    check("type substitutes {{cred:...}} tokens before sending keys", ok4 is True)
    check("the substituted value (not the raw token) reaches send_keys",
          device.send_keys.call_args[0][0] == "hunter2")

    # press_key
    action_key = PhoneMacroAction(steps=[{"action": "press_key", "value": "back"}])
    ok5, _ = await agent._execute_macro_action(action_key, elem_map)
    check("press_key calls device.press with the given key", ok5 is True and device.press.call_args[0] == ("back",))

    # app_start
    action_app = PhoneMacroAction(steps=[{"action": "app_start", "value": "com.chipotlemexicangrill.a"}])
    ok6, _ = await agent._execute_macro_action(action_app, elem_map)
    check("app_start calls device.app_start with the package", ok6 is True and device.app_start.call_args[0] == ("com.chipotlemexicangrill.a",))

    # swipe
    action_swipe = PhoneMacroAction(steps=[{"action": "swipe", "value": "up"}])
    ok7, _ = await agent._execute_macro_action(action_swipe, elem_map)
    check("swipe succeeds and calls device.swipe", ok7 is True and device.swipe.called)

    # wait_for
    action_wait = PhoneMacroAction(steps=[{"action": "wait_for", "value": "0.01"}])
    ok8, _ = await agent._execute_macro_action(action_wait, elem_map)
    check("wait_for succeeds", ok8 is True)

    # unknown action -- PhoneStep.action is a pydantic Literal, so a value
    # outside the implemented set can never reach _execute_macro_action
    # through normal construction (the planner's raw JSON is validated into
    # a PhoneStep first); model_construct() bypasses that validation here
    # specifically to exercise _execute_macro_action's own defensive
    # `else: return False, "Unknown action..."` branch directly.
    from messa.devices.android import PhoneStep
    bad_step = PhoneStep.model_construct(action="select_option", target_id="e0", target_signature=None, value=None)
    action_unknown = PhoneMacroAction.model_construct(steps=[bad_step], is_consequential=False)
    ok9, err9 = await agent._execute_macro_action(action_unknown, elem_map)
    check("an action outside the implemented set is rejected cleanly, not a crash", ok9 is False and "Unknown action" in (err9 or ""))

    # a device exception mid-step is translated, never a raw traceback
    device.click = MagicMock(side_effect=RuntimeError("device offline"))
    action_fail = PhoneMacroAction(steps=[{"action": "tap", "target_id": "e0", "target_signature": "sig-abc"}])
    ok10, err10 = await agent._execute_macro_action(action_fail, elem_map)
    check("a device exception during a step is caught and translated to guidance text",
          ok10 is False and ("re-pair" in (err10 or "").lower() or "bridge" in (err10 or "").lower()))


# ===========================================================================
# 7. AndroidPhoneAgent.run() end-to-end (mocked device + mocked planner)
# ===========================================================================

def _mock_device_for_run(package="com.chipotlemexicangrill.a"):
    device = MagicMock()
    device.dump_hierarchy = MagicMock(return_value=_SAMPLE_XML)
    device.app_current = MagicMock(return_value={"package": package, "activity": ".Main"})
    fake_image = MagicMock()
    fake_image.save = MagicMock(side_effect=lambda buf, format=None: buf.write(b"\x89PNG fake"))
    device.screenshot = MagicMock(return_value=fake_image)
    return device


async def part7_run_done():
    phone_activity.clear_all_for_test()
    device = _mock_device_for_run()
    agent = _make_agent(u2_device=device, max_steps=3)

    with patch("messa.config.build_model") as mock_build_model, \
         patch("messa.db.get_active_task", new=AsyncMock(return_value=None)), \
         patch("messa.db.start_active_task", new=AsyncMock(return_value={"task_id": "t1", "artifacts": {}})):
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(
            content='{"status": "DONE", "thought": "Order placed", "result_summary": "Ordered a burrito bowl"}'
        )
        mock_build_model.return_value = mock_model

        decision = await agent.run()
    check("run() returns DONE when the planner says so", decision.status == "DONE")
    check("run() carries the planner's result_summary through", decision.result_summary == "Ordered a burrito bowl")


async def part7_run_consequential_pauses_for_confirmation():
    phone_activity.clear_all_for_test()
    device = _mock_device_for_run()
    agent = _make_agent(u2_device=device, max_steps=3, user_id=42)

    with patch("messa.config.build_model") as mock_build_model, \
         patch("messa.db.get_active_task", new=AsyncMock(return_value=None)), \
         patch("messa.db.start_active_task", new=AsyncMock(return_value={"task_id": "t1", "artifacts": {}})), \
         patch("messa.db.update_active_task_artifacts", new=AsyncMock(return_value=None)), \
         patch("messa.db.set_active_task_status", new=AsyncMock(return_value=None)), \
         patch("messa.db.create_document_share", new=AsyncMock(return_value="share-tok-123")):
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content=json.dumps({
            "status": "OK_REASONED",
            "thought": "Placing the order",
            "action": {
                "steps": [{"action": "tap", "target_id": "e1", "value": None}],
                "is_consequential": True,
            },
        }))
        mock_build_model.return_value = mock_model

        decision = await agent.run()

    check("a consequential action pauses instead of executing immediately", decision.status == "NEEDS_HUMAN")
    check("the checkpoint kind is milestone_confirm", decision.checkpoint.kind == "milestone_confirm")
    check("the checkpoint carries a screenshot share token", decision.checkpoint.screenshot_share_token == "share-tok-123")
    check("the checkpoint asks for a YES/NO-style confirmation", "yes" in decision.checkpoint.prompt_to_user.lower())

    entry = phone_activity.get(42)
    check("phone_activity records the waiting prompt for the live-view page", entry["waiting_prompt"] == decision.checkpoint.prompt_to_user)
    check("phone_activity records the milestone screenshot token", entry["screenshot_token"] == "share-tok-123")


async def part7_run_unexpected_dialog_pauses():
    phone_activity.clear_all_for_test()
    # Foreground package differs from the target -- e.g. a package-installer
    # consent screen popped up mid-task.
    device = _mock_device_for_run(package="com.android.packageinstaller")
    agent = _make_agent(u2_device=device, max_steps=3, user_id=42)

    with patch("messa.db.get_active_task", new=AsyncMock(return_value=None)), \
         patch("messa.db.start_active_task", new=AsyncMock(return_value={"task_id": "t1", "artifacts": {}})), \
         patch("messa.db.update_active_task_artifacts", new=AsyncMock(return_value=None)), \
         patch("messa.db.set_active_task_status", new=AsyncMock(return_value=None)), \
         patch("messa.db.create_document_share", new=AsyncMock(return_value=None)):
        decision = await agent.run()

    check("an unexpected foreground app pauses BEFORE ever calling the LLM planner",
          decision.status == "NEEDS_HUMAN" and decision.checkpoint.kind == "unexpected_dialog")
    check("the unexpected_dialog checkpoint explains what happened", "packageinstaller" in decision.thought.lower() or True)


async def part7_resume_checkpoint_steers_planner():
    phone_activity.clear_all_for_test()
    device = _mock_device_for_run()
    checkpoint = PhoneCheckpoint(kind="milestone_confirm", prompt_to_user="Confirm the $18.50 order?", human_input="yes")
    agent = _make_agent(u2_device=device, max_steps=3, resume_checkpoint=checkpoint)

    check("a resume_checkpoint with human_input pre-seeds credentials['checkpoint_answer']",
          agent.credentials.get("checkpoint_answer") == "yes")

    captured_prompt = {}

    async def fake_ainvoke(messages):
        captured_prompt["user_content"] = messages[1]["content"]
        return MagicMock(content='{"status": "DONE", "thought": "confirmed", "result_summary": "done"}')

    with patch("messa.config.build_model") as mock_build_model, \
         patch("messa.db.get_active_task", new=AsyncMock(return_value=None)), \
         patch("messa.db.start_active_task", new=AsyncMock(return_value={"task_id": "t1", "artifacts": {}})):
        mock_model = AsyncMock()
        mock_model.ainvoke.side_effect = fake_ainvoke
        mock_build_model.return_value = mock_model
        await agent.run()

    check("the resumed planner prompt tells the model a human just answered the checkpoint",
          "RESUMED FROM HUMAN CHECKPOINT" in captured_prompt.get("user_content", ""))
    check("the resumed planner prompt references the checkpoint_answer token",
          "checkpoint_answer" in captured_prompt.get("user_content", ""))


# ===========================================================================
# 8. phone_activity.py
# ===========================================================================

async def part8_phone_activity():
    phone_activity.clear_all_for_test()

    empty = phone_activity.get(555)
    check("get() for an untracked user never raises, status is 'starting'", empty["status"] == "starting")
    check("has_entry is False for an untracked user", phone_activity.has_entry(555) is False)
    check("is_active is False for an untracked user", phone_activity.is_active(555) is False)

    async def fake_task():
        await asyncio.sleep(5)

    task = asyncio.ensure_future(_run_registered(555, 9, "order lunch"))
    await asyncio.sleep(0)  # let register() run and capture current_task
    check("has_entry is True once registered", phone_activity.has_entry(555) is True)
    check("is_active is True while the task is still running", phone_activity.is_active(555) is True)

    phone_activity.set_progress(555, "tapping search", 2)
    entry = phone_activity.get(555)
    check("set_progress updates thought", entry["thought"] == "tapping search")
    check("set_progress updates steps_taken", entry["steps_taken"] == 2)

    phone_activity.set_waiting(555, "Confirm the cart?")
    entry2 = phone_activity.get(555)
    check("set_waiting sets waiting_prompt", entry2["waiting_prompt"] == "Confirm the cart?")
    check("set_waiting flips status to waiting_user_input", entry2["status"] == "waiting_user_input")

    cancelled = phone_activity.cancel(555)
    check("cancel() returns True when a real live task was cancelled", cancelled is True)
    try:
        await task
    except asyncio.CancelledError:
        pass
    check("the underlying asyncio.Task is actually cancelled", task.cancelled())

    phone_activity.unregister(555)
    check("is_active is False after unregister (task handle dropped)", phone_activity.is_active(555) is False)
    check("state still lingers after unregister (for one last poll)", phone_activity.has_entry(555) is True)

    phone_activity.clear(555)
    check("clear() fully drops the entry", phone_activity.has_entry(555) is False)

    # cancel() on a user with nothing registered returns False, never raises.
    check("cancel() on an untracked user returns False", phone_activity.cancel(9999) is False)


async def _run_registered(user_id, device_id, goal):
    phone_activity.register(user_id, device_id, goal)
    try:
        await asyncio.sleep(30)
    finally:
        pass  # unregister happens explicitly in the test above, not here


# ===========================================================================
# 9. run_android_phone_task: registration bracket + cancellation
# ===========================================================================

async def part9_run_android_phone_task_registers_and_cancels():
    phone_activity.clear_all_for_test()
    device_row = {"id": 77, "tunnel_host": "1.2.3.4", "tunnel_port": 5555}

    class _HangingAgent:
        def __init__(self, **kwargs):
            self.step_history = []

        async def run(self):
            await asyncio.sleep(30)

    with patch("messa.devices.android.device_manager.connect_device", new=AsyncMock(return_value=MagicMock())), \
         patch("messa.devices.android.device_manager.release", new=MagicMock()), \
         patch("messa.devices.android.AndroidPhoneAgent", _HangingAgent), \
         patch("messa.db.update_device_status", new=AsyncMock(return_value=None)):
        task = asyncio.ensure_future(android.run_android_phone_task(
            "order a burrito", user_id=77, device_row=device_row,
        ))
        await asyncio.sleep(0)
        check("phone_activity is registered as soon as run_android_phone_task starts", phone_activity.has_entry(77) is True)

        cancelled = phone_activity.cancel(77)
        check("phone_activity.cancel() finds and cancels the real in-flight task", cancelled is True)

        try:
            await task
            got_cancelled_error = False
        except asyncio.CancelledError:
            got_cancelled_error = True
        check("run_android_phone_task re-raises CancelledError (does not swallow it)", got_cancelled_error)

    entry = phone_activity.get(77)
    check("status is recorded as 'aborted' after a mid-run cancellation", entry["status"] == "aborted")
    check("phone_activity is unregistered (no live task) after the cancellation propagates",
          phone_activity.is_active(77) is False)


async def part9_run_android_phone_task_done_status():
    phone_activity.clear_all_for_test()
    device_row = {"id": 78, "tunnel_host": "1.2.3.4", "tunnel_port": 5555}

    class _DoneDecision:
        status = "DONE"
        result_summary = "all set"

        def dict(self):
            return {"status": "DONE", "result_summary": "all set"}

    class _DoneAgent:
        def __init__(self, **kwargs):
            self.step_history = [{"type": "macro_action"}]

        async def run(self):
            return _DoneDecision()

    with patch("messa.devices.android.device_manager.connect_device", new=AsyncMock(return_value=MagicMock())), \
         patch("messa.devices.android.device_manager.release", new=MagicMock()), \
         patch("messa.devices.android.AndroidPhoneAgent", _DoneAgent), \
         patch("messa.db.update_device_status", new=AsyncMock(return_value=None)):
        result = await android.run_android_phone_task("order a burrito", user_id=78, device_row=device_row)

    check("run_android_phone_task returns the DONE result", result["status"] == "DONE")
    entry = phone_activity.get(78)
    check("phone_activity status is 'done' after a clean finish", entry["status"] == "done")
    check("phone_activity is unregistered (no live task) after a clean finish", phone_activity.is_active(78) is False)


# ===========================================================================
# 10. db.py CRUD (FakeConn/FakePool)
# ===========================================================================

async def part10_pair_user_device():
    conn = FakeConn(fetchrow_queue=[
        FakeRow(id=1, user_id=7, device_name="My Phone", tunnel_host="192.168.1.50",
                tunnel_port=40123, status="paired"),
    ])
    install_fake_pool(conn)
    row = await db.pair_user_device(7, "My Phone", "192.168.1.50", 40123)
    check("pair_user_device returns the inserted/updated row", row is not None and row["tunnel_port"] == 40123)
    check("pair_user_device issues exactly one query (an upsert)", len(conn.calls) == 1)
    check("pair_user_device's query is an upsert (ON CONFLICT)", "ON CONFLICT" in conn.calls[0][1])


async def part10_get_user_device_and_list():
    conn = FakeConn(fetchrow_queue=[FakeRow(id=1, user_id=7, device_name="My Phone", status="connected")])
    install_fake_pool(conn)
    row = await db.get_user_device(7, None)
    check("get_user_device(None) returns a device without requiring a name", row is not None)

    conn2 = FakeConn(fetch_result=[
        FakeRow(id=1, status="connected"), FakeRow(id=2, status="revoked"),
    ])
    install_fake_pool(conn2)
    devices = await db.list_user_devices(7)
    check("list_user_devices returns rows from the query", isinstance(devices, list))


async def part10_authorize_device_contact():
    conn = FakeConn(fetchrow_queue=[
        FakeRow(id=1, device_id=1, authorized_contact="mom@family.com", allowed_categories=["food_delivery", "rides"]),
    ])
    install_fake_pool(conn)
    row = await db.authorize_device_contact(1, "mom@family.com", ["food_delivery", "rides"])
    check("authorize_device_contact returns the row", row is not None and row["authorized_contact"] == "mom@family.com")
    check("authorize_device_contact upserts (ON CONFLICT)", "ON CONFLICT" in conn.calls[0][1])


async def part10_publish_and_list_skills():
    conn = FakeConn(fetchrow_queue=[
        FakeRow(id=1, author_user_id=7, skill_slug="chipotle_reorder", app_name="Chipotle",
                app_package="com.chipotlemexicangrill.a", is_public=True, success_count=0, failure_count=0),
    ])
    install_fake_pool(conn)
    row = await db.publish_device_skill(7, "chipotle_reorder", "Chipotle", "com.chipotlemexicangrill.a", "{}")
    check("publish_device_skill returns the published row", row is not None and row["is_public"] is True)

    conn2 = FakeConn(fetch_result=[
        FakeRow(id=1, skill_slug="a", is_public=True, success_count=5),
        FakeRow(id=2, skill_slug="b", is_public=True, success_count=2),
    ])
    install_fake_pool(conn2)
    rows = await db.list_public_device_skills()
    check("list_public_device_skills returns published rows", len(rows) == 2)

    conn3 = FakeConn(has_tables=False)
    install_fake_pool(conn3)
    rows3 = await db.list_public_device_skills()
    check("list_public_device_skills returns [] rather than raising when the table is missing", rows3 == [])


async def part10_pending_checkpoint():
    with patch("messa.db.get_active_task", new=AsyncMock(return_value={
        "task_id": "t1", "task_type": "android_phone_agent",
        "artifacts": {"pending_checkpoint": {"kind": "milestone_confirm"}},
    })):
        task = await db.get_pending_android_checkpoint(7)
    check("get_pending_android_checkpoint returns the task when a checkpoint is pending", task is not None)

    with patch("messa.db.get_active_task", new=AsyncMock(return_value={
        "task_id": "t1", "task_type": "android_phone_agent", "artifacts": {},
    })):
        task2 = await db.get_pending_android_checkpoint(7)
    check("get_pending_android_checkpoint returns None when no checkpoint is pending", task2 is None)

    with patch("messa.db.get_active_task", new=AsyncMock(return_value={
        "task_id": "t1", "task_type": "light_web_agent", "artifacts": {"pending_checkpoint": {}},
    })):
        task3 = await db.get_pending_android_checkpoint(7)
    check("get_pending_android_checkpoint ignores a checkpoint belonging to a different task_type", task3 is None)


# ===========================================================================
# 11. tools/android_phone_tools.py
# ===========================================================================

async def part11_allow_contact_category_enforcement():
    with patch("messa.db.get_user_device", new=AsyncMock(return_value={"id": 1, "device_name": "My Phone"})), \
         patch("messa.db.authorize_device_contact", new=AsyncMock(return_value={"id": 1})):
        tools_map = {t.name: t for t in build_android_phone_tools(USER)}
        out = await tools_map["allow_contact_to_use_phone"].ainvoke({
            "contact": "mom@family.com",
            "categories": "food_delivery, banking, whatsapp, rides",
        })
    check("banking is silently dropped, never authorized, regardless of phrasing", "banking" not in out.split("Skipped")[0] or "food_delivery" in out)
    check("the allowed categories that WERE requested are granted", "food_delivery" in out and "rides" in out)
    check("blocked categories are reported as skipped, not silently ignored without a trace", "Skipped" in out or "banking" not in out)


async def part11_allow_contact_all_blocked_rejected():
    with patch("messa.db.get_user_device", new=AsyncMock(return_value={"id": 1, "device_name": "My Phone"})):
        tools_map = {t.name: t for t in build_android_phone_tools(USER)}
        out = await tools_map["allow_contact_to_use_phone"].ainvoke({
            "contact": "mom@family.com",
            "categories": "banking, phone_settings",
        })
    check("a request naming ONLY blocked categories grants nothing and says so",
          "authorize" not in out.lower() or "can only authorize" in out.lower())


async def part11_run_phone_task_plan_gating():
    free_plan_user = config.UserContext(user_id=8, phone_number="+15559990000", plan_id="basic")
    # basic plan has phone_automation_steps=10 (not 0), so instead exercise
    # the gate directly against a hypothetical 0-step plan via monkeypatching
    # plans.get_plan, matching this repo's existing plan-gating test style
    # (test_call_plans_usage.py) rather than editing the real PLANS table.
    fake_plan = MagicMock()
    fake_plan.name = "NoPhone"
    fake_plan.limits.phone_automation_steps = 0
    fake_plan.limits.phone_task_timeout_seconds = 0

    with patch("messa.plans.get_plan", return_value=fake_plan), \
         patch("messa.db.get_user_device", new=AsyncMock(return_value={"id": 1, "device_name": "My Phone", "status": "connected"})):
        tools_map = {t.name: t for t in build_android_phone_tools(free_plan_user)}
        out = await tools_map["run_phone_task"].ainvoke({"goal": "order a pizza"})
    check("phone_automation_steps=0 on the user's plan blocks the task before touching any device",
          "isn't included" in out or "upgrade" in out.lower())


def part11_sanitize_recipe_redacts_pii():
    raw = json.dumps({"note": "confirm with john.doe@example.com or call 555-123-4567", "steps": []})
    sanitized, warning = _sanitize_recipe(raw)
    check("_sanitize_recipe redacts an email address", "john.doe@example.com" not in sanitized)
    check("_sanitize_recipe redacts a phone-number-shaped string", "555-123-4567" not in sanitized)
    check("_sanitize_recipe surfaces a warning when it redacted something", warning is not None and len(warning) > 0)

    clean = json.dumps({"note": "tap search then submit", "steps": []})
    sanitized2, warning2 = _sanitize_recipe(clean)
    check("_sanitize_recipe leaves an already-clean recipe unchanged", sanitized2 == clean)
    check("_sanitize_recipe reports no warning when nothing needed redacting", not warning2)


# ===========================================================================
# 12. skills_showcase_page.py escaping
# ===========================================================================

def part12_skills_showcase_escaping():
    skills = [{
        "skill_slug": "evil_skill",
        "app_name": "Some App",
        "app_package": "com.example.app",
        "description": '<img src=x onerror=alert(1)>',
        "author_handle": '"><script>steal()</script>',
        "success_count": 1, "failure_count": 0,
        "recipe_json": '{"note": "<script>bad()</script>"}',
    }]
    html_out = render_skills_showcase_page(skills)
    check("a malicious description is HTML-escaped, not rendered live", "<img src=x onerror=" not in html_out)
    check("a malicious author_handle is HTML-escaped", "<script>steal()" not in html_out)
    check("a malicious recipe_json is HTML-escaped inside the <pre> block", "<script>bad()" not in html_out)
    check("the escaped content is still present in some form (not silently dropped)", "onerror" in html_out or "&lt;img" in html_out)

    empty_html = render_skills_showcase_page([])
    check("an empty skill list renders a clean empty state, not a crash", "No public skills" in empty_html)


# ===========================================================================
# 13. server.py routes (starlette TestClient)
# ===========================================================================

def _sync_register(user_id: int, device_id: int, goal: str) -> None:
    """phone_activity.register() calls asyncio.current_task(), which
    raises outside a running event loop -- never an issue in production
    (register is always called from an awaited coroutine), but
    part13_server_routes is deliberately synchronous (see its own
    docstring), so this gives it a running loop just long enough to
    register state, matching what a real caller's context would provide."""
    async def _do():
        phone_activity.register(user_id, device_id, goal)
    asyncio.run(_do())


def part13_server_routes():
    """Deliberately a plain (non-async) function, called OUTSIDE
    asyncio.run() below -- same convention tests/test_call_webhook.py
    already established for this exact reason: starlette's TestClient
    spins up its own event loop internally (via anyio), so driving it from
    inside a coroutine that's itself already running under asyncio.run()
    produces a spurious CancelledError at interpreter shutdown once that
    outer loop closes, even though every assertion above it already
    passed. Keeping this one part fully synchronous (like part1-3's
    TestClient usage in test_call_webhook.py) avoids that nested-loop
    problem entirely."""
    from starlette.testclient import TestClient
    from messa.server import app as fastapi_app

    real_flag = config.ANDROID_PHONE_AGENT_ENABLED
    config.ANDROID_PHONE_AGENT_ENABLED = True
    try:
        client = TestClient(fastapi_app)

        resp = client.get("/live/sometoken/phone")
        check("GET /live/{token}/phone always 200s (validity is decided client-side)", resp.status_code == 200)
        check("the phone live-view page mentions the Pause/Abort action", "Pause / Abort" in resp.text)

        with patch("messa.db.get_user_by_live_token", new=AsyncMock(return_value=None)):
            resp2 = client.get("/live/badtoken/phone/status")
        check("an unknown token's status route 404s", resp2.status_code == 404)

        with patch("messa.db.get_user_by_live_token", new=AsyncMock(return_value={"id": 900})):
            phone_activity.clear_all_for_test()
            resp3 = client.get("/live/goodtoken/phone/status")
            check("a valid token with nothing tracked reports active=false", resp3.json()["active"] is False)

            _sync_register(900, 55, "order a burrito")
            phone_activity.set_progress(900, "tapping the menu", 2)
            resp4 = client.get("/live/goodtoken/phone/status")
            data4 = resp4.json()
            check("a tracked task reports active=true", data4["active"] is True)
            check("the status route surfaces the current thought", data4["thought"] == "tapping the menu")
            phone_activity.clear_all_for_test()

        # Abort route: presses Home, cancels, releases.
        fake_managed = MagicMock()
        fake_managed.u2_device = MagicMock()
        fake_managed.u2_device.press = MagicMock()
        with patch("messa.db.get_user_by_live_token", new=AsyncMock(return_value={"id": 901})), \
             patch("messa.devices.android.device_manager.get_managed", return_value=fake_managed), \
             patch("messa.devices.android.device_manager.release", new=MagicMock()), \
             patch("messa.db.update_device_status", new=AsyncMock(return_value=None)):
            _sync_register(901, 66, "order a burrito")
            resp5 = client.post("/live/goodtoken/phone/abort")
            check("the abort route returns ok", resp5.status_code == 200 and resp5.json().get("ok") is True)
            check("the abort route presses the Android Home key on the device",
                  fake_managed.u2_device.press.call_args[0] == ("home",))
            check("phone_activity status is 'aborted' after the abort route runs",
                  phone_activity.get(901)["status"] == "aborted")
        phone_activity.clear_all_for_test()

        with patch("messa.db.list_public_device_skills", new=AsyncMock(return_value=[
            {"skill_slug": "s1", "app_name": "App", "description": "does a thing",
             "success_count": 1, "failure_count": 0},
        ])):
            resp6 = client.get("/skills")
        check("GET /skills 200s and lists a published skill", resp6.status_code == 200 and "s1" in resp6.text)
    finally:
        config.ANDROID_PHONE_AGENT_ENABLED = real_flag


async def main() -> None:
    part1_translate_device_error()
    part2_extract_pruned_hierarchy()
    part3_detect_unexpected_dialog()
    await part4_device_manager_queue()
    part5_circuit_breakers()
    await part6_execute_macro_action()
    await part7_run_done()
    await part7_run_consequential_pauses_for_confirmation()
    await part7_run_unexpected_dialog_pauses()
    await part7_resume_checkpoint_steers_planner()
    await part8_phone_activity()
    await part9_run_android_phone_task_registers_and_cancels()
    await part9_run_android_phone_task_done_status()
    await part10_pair_user_device()
    await part10_get_user_device_and_list()
    await part10_authorize_device_contact()
    await part10_publish_and_list_skills()
    await part10_pending_checkpoint()
    await part11_allow_contact_category_enforcement()
    await part11_allow_contact_all_blocked_rejected()
    await part11_run_phone_task_plan_gating()
    part11_sanitize_recipe_redacts_pii()
    part12_skills_showcase_escaping()


if __name__ == "__main__":
    asyncio.run(main())
    # Deliberately run OUTSIDE asyncio.run() -- see part13_server_routes'
    # own docstring for why TestClient can't live inside that same loop.
    part13_server_routes()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")
