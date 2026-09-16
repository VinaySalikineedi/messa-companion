"""Open-Source Phone / Bring Your Own Phone (BYOP) -- pairs and drives a
user's own Android phone over ADB wireless debugging (open-source-
phone.md, feature/open-source-phone).

Two layers, same split as messa/channels/browser_session_manager.py +
light_web_agent.py:

1. AndroidDeviceManager (this module) -- pairing, connection, and the
   per-device mutex queue (open-source-phone.md section 4: a phone can
   only run one app in the foreground, so concurrent requests queue
   rather than corrupt each other's UI state). A process-wide singleton,
   `device_manager`, mirrors browser_session_manager.session_manager.

2. AndroidPhoneAgent (this module) -- the actual Sense-Reflect-Act-Verify
   automation loop over the device's UI hierarchy, deliberately shaped
   like messa/channels/light_web_agent.py's LightWebAgent: same idea
   (perceive the screen, let an LLM decide a small batch of actions,
   execute, verify, repeat), same human-checkpoint suspend/resume pattern
   reusing the Active Task Scratchpad (db.get_pending_android_checkpoint
   is the sibling of db.get_pending_light_web_checkpoint), same circuit
   breakers. Ported concepts, not copy-pasted code -- a phone's UI
   hierarchy and a browser's DOM are different enough (no CSS occlusion
   test, no URL, real hardware keys) that this is its own module rather
   than a shared base class with the browser engine.

adbutils' own pure-Python AdbClient does NOT implement the wireless-
debugging PAIRING handshake (only the plain host:port connect protocol)
-- see requirements.txt's own comment on this. pair_device below shells
out to the real `adb` binary adbutils bundles (adbutils.adb_path()) just
for that one `adb pair host:port code` command; everything after pairing
uses adbutils'/uiautomator2's pure-Python clients.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from .. import config, db, phone_activity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Conversational error translation (open-source-phone.md section 3) --
# every raw adbutils/uiautomator2 exception a pairing/connect/action call
# can throw gets mapped here to a (short_code, human-friendly text) pair.
# NEVER let a raw traceback or exception repr reach a user over SMS --
# that's the entire point of this table existing as its own thing instead
# of just `str(exc)`.
# ---------------------------------------------------------------------------

_ERROR_GUIDANCE: list[tuple[re.Pattern, str, str]] = [
    (
        re.compile(r"failed to authenticate|auth.*fail|incorrect.*pin|pairing.*fail", re.I),
        "pairing_code_expired",
        "The 6-digit pairing code expired. On your phone, go to Settings > Developer Options > "
        "Wireless debugging > Pair device with pairing code, and text me the new 6 digits.",
    ),
    (
        re.compile(r"connection refused|econnrefused", re.I),
        "wireless_debugging_disabled",
        "It looks like Wireless Debugging turned off (Android disables this automatically when "
        "reconnecting to Wi-Fi). Go to Settings > Developer Options > Wireless Debugging and toggle "
        "it back on, then text me \"reconnect\".",
    ),
    (
        re.compile(r"timed out|timeout|no route to host|network is unreachable", re.I),
        "device_unreachable",
        "I can't reach your phone right now. Please make sure the Messa Companion app is open "
        "and shows 'Connected', then try your command again.",
    ),
    (
        re.compile(r"device.*offline|not found|no such device", re.I),
        "device_offline",
        "Your phone looks offline or the pairing expired. Please re-pair: text me "
        "\"connect <ip:port> <code>\" with the fresh values from Developer Options > Wireless Debugging.",
    ),
    (
        re.compile(r"hierarchy.*empty|hierarchyempty", re.I),
        "screen_locked_or_blank",
        "I can't read your screen right now -- it may be locked or off. Please unlock your phone "
        "with your PIN or fingerprint so I can continue.",
    ),
]

_DEFAULT_ERROR_GUIDANCE = (
    "unknown_device_error",
    "I ran into a problem talking to your phone. Please make sure it's unlocked, on Wi-Fi, and "
    "Wireless Debugging is still on, then text me \"reconnect\".",
)


def translate_device_error(exc: Exception) -> tuple[str, str]:
    """Maps a raw adbutils/uiautomator2 exception to (short_code, plain-
    English guidance) -- see _ERROR_GUIDANCE's own header comment. Always
    returns something, never raises -- a translation failure must never
    itself become the thing that crashes an error-handling path."""
    text = f"{type(exc).__name__}: {exc}"
    for pattern, code, guidance in _ERROR_GUIDANCE:
        if pattern.search(text):
            return code, guidance
    return _DEFAULT_ERROR_GUIDANCE


# ---------------------------------------------------------------------------
# Structured action primitives (mirrors light_web_agent.py's MacroStep/
# MacroAction shape, own pydantic models -- a phone's action vocabulary
# is genuinely different: taps by screen coordinate, hardware keys,
# swipes, and app launches by package name, not clicks/types on a DOM
# element).
# ---------------------------------------------------------------------------

class PhoneStep(BaseModel):
    action: Literal["tap", "type", "press_key", "swipe", "app_start", "wait_for"]
    target_id: Optional[str] = None          # e.g. "e3", from the pruned hierarchy
    target_signature: Optional[str] = None   # staleness check, same idea as light_web_agent's
    value: Optional[str] = None              # text to type / key name / package name / swipe direction


class PhoneMacroAction(BaseModel):
    steps: List[PhoneStep] = Field(default_factory=list)
    is_consequential: bool = False   # routes through the milestone-confirm HITL gate if True


class PhoneCheckpoint(BaseModel):
    kind: Literal["milestone_confirm", "unexpected_dialog", "device_offline", "needs_help"]
    prompt_to_user: str
    interactive_url: Optional[str] = None
    timeout_seconds: int = 300
    trigger_reason: Optional[str] = None
    human_input: Optional[str] = None
    screenshot_share_token: Optional[str] = None


class PhoneAgentDecision(BaseModel):
    status: Literal["OK_REASONED", "NEEDS_HUMAN", "DONE", "FAILED"]
    thought: Optional[str] = None
    action: Optional[PhoneMacroAction] = None
    checkpoint: Optional[PhoneCheckpoint] = None
    result_summary: Optional[str] = None
    scratchpad_updates: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# AndroidDeviceManager -- pairing, connection, per-device mutex queue
# ---------------------------------------------------------------------------

class QueueFullError(Exception):
    """Raised when a device's queue is already at config.
    ANDROID_PHONE_MAX_QUEUE_DEPTH -- open-source-phone.md's queue is meant
    to smooth out "mom's ordering groceries, I'll wait 2 minutes," not
    let a dozen unrelated requests pile up against one physical phone."""


class ManagedDevice:
    """One paired phone's live connection state -- mirrors
    browser_session_manager.ManagedBrowserSession's shape closely (a
    live driver handle, a status, a touch()-refreshed last-seen clock),
    but for a real device instead of a cloud browser session."""

    def __init__(self, device_id: int, tunnel_host: str, tunnel_port: int) -> None:
        self.device_id = device_id
        self.tunnel_host = tunnel_host
        self.tunnel_port = tunnel_port
        self.u2_device: Any = None          # uiautomator2.Device once connected
        self.status: str = "paired"         # paired | connected | busy | offline
        self.lock = asyncio.Lock()
        self.queue_depth = 0
        self.last_seen = time.time()
        self.checkpoint: Optional[dict] = None

    def touch(self) -> None:
        self.last_seen = time.time()

    @property
    def addr(self) -> str:
        return f"{self.tunnel_host}:{self.tunnel_port}"


class DeviceQueueTicket:
    """Async context manager returned by AndroidDeviceManager.acquire --
    reports the caller's queue position (0 = running now) BEFORE blocking,
    so a caller (tools/android_phone_tools.py) can tell the requester
    "you're #1 in queue, ~2 min" the way open-source-phone.md section 4
    describes, rather than just hanging silently."""

    def __init__(self, managed: ManagedDevice, position: int) -> None:
        self._managed = managed
        self.position = position

    async def __aenter__(self) -> ManagedDevice:
        try:
            await self._managed.lock.acquire()
        except BaseException:
            self._managed.queue_depth = max(0, self._managed.queue_depth - 1)
            raise
        self._managed.status = "busy"
        self._managed.touch()
        return self._managed

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._managed.status = "connected"
        self._managed.lock.release()
        self._managed.queue_depth = max(0, self._managed.queue_depth - 1)


class AndroidDeviceManager:
    """Process-wide singleton (see `device_manager` below) tracking every
    currently-connected phone. Deliberately in-memory only, same tradeoff
    browser_session_manager.BrowserSessionManager makes: a real live ADB
    TCP connection can't be serialized/resumed across a process restart
    anyway, so persisting connection state to Postgres would be a lie --
    what IS persisted (user_devices' tunnel_host/tunnel_port, and a
    suspended task's checkpoint via active_tasks) is exactly the subset
    that's actually durable."""

    def __init__(self) -> None:
        self._devices: Dict[int, ManagedDevice] = {}

    async def pair_device(
        self, host: str, port: int, code: str, discover_timeout: float = 6.0,
    ) -> tuple[bool, str, Optional[int]]:
        """Runs `adb pair host:port code` via the bundled adb binary (see
        this module's own docstring for why adbutils' Python client can't
        do this step itself). Returns (ok, message, discovered_connect_port).

        Android's wireless-debugging pairing port is ONE-TIME and separate
        from the device's ONGOING connect port (Developer Options shows
        both, as different numbers, on the same IP) -- after a successful
        `adb pair`, modern Android/adb auto-connects over mDNS on the same
        local adb server this bundled binary spawns, so a plain
        `adbutils.AdbClient().device_list()` poll right after pairing will
        usually surface that connect port directly without the user having
        to hunt for and text back a second number. discovered_connect_port
        is that auto-discovered port, or None if nothing showed up within
        discover_timeout (mDNS doesn't always reach across every network,
        e.g. between a Mac and a phone on different VLANs) -- callers
        (tools/android_phone_tools.py's pair_my_phone) fall back to asking
        the user for the "IP address & Port" shown on the main Wireless
        Debugging screen in that case."""
        import adbutils

        adb_path = adbutils.adb_path()
        addr = f"{host}:{port}"
        try:
            proc = await asyncio.create_subprocess_exec(
                adb_path, "pair", addr, code,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            return False, translate_device_error(TimeoutError("adb pair timed out"))[1], None
        except Exception as e:  # noqa: BLE001 - adb binary missing/unexecutable, etc.
            return False, translate_device_error(e)[1], None

        output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
        if not ("Successfully paired" in output or proc.returncode == 0):
            code_name, guidance = translate_device_error(RuntimeError(output))
            logger.warning(f"[AndroidDeviceManager] pair_device failed ({code_name}): {output}")
            return False, guidance, None

        discovered_port = await self._discover_connect_port(host, discover_timeout)
        return True, output or "Paired successfully.", discovered_port

    async def _discover_connect_port(self, host: str, timeout: float) -> Optional[int]:
        """Polls the local adb server's device list for a serial on `host`
        (mDNS auto-connect after a successful pair) -- see pair_device's
        own docstring. Best-effort: any failure here just means "nothing
        discovered," never raises."""
        import adbutils

        deadline = time.time() + timeout
        client = adbutils.AdbClient()
        while time.time() < deadline:
            try:
                devices = await asyncio.to_thread(client.device_list)
            except Exception:
                devices = []
            for d in devices:
                serial = getattr(d, "serial", "") or ""
                if serial.startswith(f"{host}:"):
                    try:
                        return int(serial.split(":", 1)[1])
                    except ValueError:
                        continue
            await asyncio.sleep(0.5)
        return None

    async def connect_device(self, device_row: dict) -> Any:
        """Connects (or reuses an existing live connection) and returns a
        uiautomator2.Device. Routes to one of two transports depending on
        device_row['bridge_kind'] (migration 044, feature/messa-
        companion-apk):

          - 'direct_lan' (default, and every device_row from before this
            column existed): dials the device's own ONGOING wireless-
            debugging tunnel_host:tunnel_port directly -- the one-time
            pairing port is a different number, handled by pair_device
            above, never this.
          - 'companion_ws': the phone has no LAN address to dial at all
            (that's the whole reason this bridge exists -- see messa/
            companion_bridge.py's own module docstring). Instead this
            looks up the live CompanionDeviceBridge the phone's own
            outbound `/device/ws` connection opened for this device_id,
            and connects to ITS local loopback port instead. If no live
            bridge exists (the Companion app isn't currently connected),
            this fails fast with a translated, user-facing message rather
            than hanging on a 127.0.0.1 port nothing is listening on.

        Either way, from here down this is a completely ordinary ADB TCP
        connect -- adbutils/uiautomator2 have no idea which transport got
        them there, which is the entire point of a pure byte-pipe bridge."""
        import uiautomator2 as u2

        device_id = device_row["id"]
        bridge_kind = device_row.get("bridge_kind") or "direct_lan"

        if bridge_kind == "companion_ws":
            from .. import companion_bridge

            bridge = companion_bridge.MANAGER.get_bridge(device_id)
            if bridge is None or bridge.local_port is None:
                raise ConnectionError(
                    "This phone's Messa Companion app isn't currently connected -- "
                    "make sure the app is open with a network connection, then try again."
                )
            host, connect_port = "127.0.0.1", bridge.local_port
        else:
            host = device_row.get("tunnel_host")
            connect_port = device_row.get("tunnel_port")
            if not host or not connect_port:
                raise ConnectionError(
                    "This phone has no known address to connect to -- try re-pairing it."
                )

        managed = self._devices.get(device_id)
        if managed is None or managed.tunnel_host != host or managed.tunnel_port != connect_port:
            managed = ManagedDevice(device_id, host, connect_port)
            self._devices[device_id] = managed

        if managed.u2_device is not None and managed.status != "offline":
            managed.touch()
            return managed.u2_device

        try:
            u2_device = await asyncio.to_thread(u2.connect, managed.addr)
            # Cheap liveness check -- catches a stale/failed connect() early
            # instead of only failing on the FIRST real action later.
            await asyncio.to_thread(lambda: u2_device.info)
        except Exception as e:
            managed.status = "offline"
            code_name, guidance = translate_device_error(e)
            logger.warning(f"[AndroidDeviceManager] connect_device failed ({code_name}): {e}")
            raise ConnectionError(guidance) from e

        managed.u2_device = u2_device
        managed.status = "connected"
        managed.touch()
        return u2_device

    def get_managed(self, device_id: int) -> Optional[ManagedDevice]:
        return self._devices.get(device_id)

    async def acquire(self, device_id: int) -> DeviceQueueTicket:
        """Returns a ticket reporting queue position BEFORE acquiring the
        lock -- see DeviceQueueTicket's own docstring. Raises
        QueueFullError if the device is already saturated (config.
        ANDROID_PHONE_MAX_QUEUE_DEPTH), so a caller can tell the requester
        to try again later instead of queuing indefinitely."""
        managed = self._devices.get(device_id)
        if managed is None:
            from .. import companion_bridge
            bridge = companion_bridge.MANAGER.get_bridge(device_id)
            if bridge is not None and bridge.local_port is not None:
                managed = ManagedDevice(device_id, "127.0.0.1", bridge.local_port)
                managed.status = "connected"
                self._devices[device_id] = managed
            else:
                row = await db.get_device_by_id(device_id)
                if row and row.get("tunnel_host") and row.get("tunnel_port"):
                    managed = ManagedDevice(device_id, row["tunnel_host"], row["tunnel_port"])
                    managed.status = "connected"
                    self._devices[device_id] = managed
                elif row and row.get("bridge_kind") == "companion_ws":
                    raise ConnectionError(
                        "This phone's Messa Companion app isn't currently connected -- "
                        "make sure the app is open with a network connection, then try again."
                    )
                else:
                    raise ConnectionError("This device isn't connected yet.")
        max_depth = getattr(config, "ANDROID_PHONE_MAX_QUEUE_DEPTH", 5)
        if managed.queue_depth >= max_depth:
            raise QueueFullError(
                f"This phone already has {managed.queue_depth} tasks waiting. Please try again shortly."
            )
        position = managed.queue_depth
        managed.queue_depth += 1
        return DeviceQueueTicket(managed, position)

    async def take_screenshot(self, device_id: int) -> Optional[bytes]:
        managed = self._devices.get(device_id)
        if managed is None or managed.u2_device is None:
            return None
        try:
            img = await asyncio.to_thread(managed.u2_device.screenshot)
        except Exception as e:
            logger.debug(f"[AndroidDeviceManager] screenshot failed: {e}")
            return None
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def release(self, device_id: int) -> None:
        """Drops the live connection handle (NOT the paired row in
        Postgres -- that's db.revoke_user_device's job, a separate,
        explicit user action). Used when a task finishes/fails and the
        device should show as merely 'connected' again, idle."""
        managed = self._devices.get(device_id)
        if managed:
            managed.status = "connected"


device_manager = AndroidDeviceManager()


# ---------------------------------------------------------------------------
# Perception: dump_hierarchy() XML -> pruned interactive-element list
# (mirrors light_web_perception.py's extract_pruned_a11y_tree, adapted for
# uiautomator2's node attributes instead of a browser a11y tree).
# ---------------------------------------------------------------------------

_INTERACTIVE_CLASSES_HINT = ("EditText", "Button", "ImageButton", "CheckBox", "Switch", "Spinner")


def _parse_bounds(bounds: str) -> tuple[int, int, int, int]:
    """'[l,t][r,b]' -> (l, t, r, b). Returns zeros on any parse failure --
    a malformed bounds string should degrade one element's usefulness, not
    crash perception for the whole screen."""
    m = re.match(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]", bounds or "")
    if not m:
        return (0, 0, 0, 0)
    return tuple(int(x) for x in m.groups())  # type: ignore[return-value]


def extract_pruned_hierarchy(xml_str: str, max_elements: int = 120) -> tuple[List[Dict[str, Any]], str]:
    """Walks dump_hierarchy()'s XML, keeps only elements a human could
    actually act on (clickable, or a text-entry field, or carries visible
    text/content-desc worth reading), assigns short ids (e1, e2, ...), and
    renders a compact text block for the LLM prompt -- the phone-hierarchy
    equivalent of light_web_perception.py's pruned a11y tree. Returns
    (elements, prompt_text)."""
    elements: List[Dict[str, Any]] = []
    prompt_lines: List[str] = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        try:
            # Fix unescaped ampersands commonly found in raw Android app hierarchy dumps (e.g. H&M, AT&T)
            fixed_xml = re.sub(r"&(?!amp;|lt;|gt;|quot;|apos;)", "&amp;", xml_str or "")
            root = ET.fromstring(fixed_xml)
        except ET.ParseError as e:
            return [], f"[Could not parse screen hierarchy: {e}]"

    idx = 0
    for node in root.iter("node"):
        if idx >= max_elements:
            break
        clickable = node.get("clickable") == "true"
        long_clickable = node.get("long-clickable") == "true"
        cls = node.get("class") or ""
        text = (node.get("text") or "").strip()
        desc = (node.get("content-desc") or "").strip()
        resource_id = node.get("resource-id") or ""
        is_edit = "EditText" in cls
        enabled = node.get("enabled") != "false"

        worth_keeping = enabled and (
            clickable or long_clickable or is_edit
            or any(hint in cls for hint in _INTERACTIVE_CLASSES_HINT)
            or bool(text) or bool(desc)
        )
        if not worth_keeping:
            continue
        # Pure decorative text (not clickable, no resource-id, no desc) is
        # noise for the ACTION list but still useful as screen context --
        # keep it out of `elements` (nothing to target) but let short
        # labels through to the prompt text as context via a lightweight
        # marker instead of a numbered element.
        if not (clickable or long_clickable or is_edit) and not resource_id and not desc:
            if text:
                prompt_lines.append(f"  (context text: \"{text[:60]}\")")
            continue

        bounds = _parse_bounds(node.get("bounds") or "")
        l, t, r, b = bounds
        cx, cy = (l + r) // 2, (t + b) // 2
        sig_src = f"{resource_id}:{text}:{desc}:{cls}:{bounds}"
        signature = hashlib.sha256(sig_src.encode()).hexdigest()[:16]
        eid = f"e{idx}"
        elements.append({
            "id": eid,
            "class": cls,
            "text": text,
            "content_desc": desc,
            "resource_id": resource_id,
            "clickable": clickable or long_clickable,
            "editable": is_edit,
            "bounds": bounds,
            "center": (cx, cy),
            "signature": signature,
        })
        label = text or desc or resource_id.split("/")[-1] or cls.split(".")[-1]
        kind = "input" if is_edit else "button" if (clickable or long_clickable) else "element"
        prompt_lines.append(f"[{eid}] {kind}: \"{label}\"" + (f" (id={resource_id})" if resource_id else ""))
        idx += 1

    prompt_text = "\n".join(prompt_lines) if prompt_lines else "(no interactive elements found on screen)"
    return elements, prompt_text


_SYSTEM_UI_PACKAGE_HINTS = (
    "com.android.systemui", "com.android.permissioncontroller", "com.google.android.permissioncontroller",
    "com.android.packageinstaller", "com.android.vending",  # Play Store update/consent prompts
)


def detect_unexpected_dialog(current_package: str, target_package: Optional[str]) -> Optional[str]:
    """The phone-hierarchy analog of light_web_agent's occlusion/modal
    detection: on a browser, an unexpected overlay is still part of the
    SAME page's DOM (that's what occlusion hit-testing is for); on
    Android, an unexpected system dialog (a permission prompt, an app
    update nag, a package-installer consent screen) is a DIFFERENT
    foreground app/activity entirely, which app_current() surfaces
    directly -- no hit-testing needed, just "is the foreground package
    something other than what I launched and not a known system-UI
    surface I should just dismiss." Returns a human-readable reason
    string when something unexpected is in front, else None."""
    if not current_package:
        return None
    if target_package and current_package == target_package:
        return None
    if any(hint in current_package for hint in _SYSTEM_UI_PACKAGE_HINTS):
        return f"An unexpected system dialog ({current_package}) is covering the screen."
    if target_package and current_package != target_package:
        return f"An unexpected app/screen ({current_package}) appeared instead of {target_package}."
    return None


# ---------------------------------------------------------------------------
# AndroidPhoneAgent -- Sense-Reflect-Act-Verify loop (mirrors
# light_web_agent.py's LightWebAgent; see this module's header comment for
# why this is a sibling engine rather than a shared base class).
# ---------------------------------------------------------------------------

class AndroidPhoneAgent:
    """Drives one already-connected uiautomator2.Device toward `user_goal`,
    batching up to a few actions per LLM turn, pausing on a
    PhoneCheckpoint (milestone confirmation, unexpected dialog, needs
    human help) via the same Active Task Scratchpad suspend/resume
    mechanism light_web_agent.py's LightWebAgent uses (see db.py's
    get_pending_android_checkpoint), and enforcing plan-tier step/timeout
    caps (open-source-phone.md section 7)."""

    def __init__(
        self,
        u2_device: Any,
        device_row: Dict[str, Any],
        user_goal: str,
        credentials: Optional[Dict[str, str]] = None,
        max_steps: int = 10,
        timeout_seconds: int = 180,
        model_name: Optional[str] = None,
        user_id: Optional[Any] = None,
        target_package: Optional[str] = None,
        resume_checkpoint: Optional[PhoneCheckpoint] = None,
    ) -> None:
        self.device = u2_device
        self.device_row = device_row
        self.user_goal = user_goal
        self.credentials = credentials or {}
        self.max_steps = max_steps
        self.timeout_seconds = timeout_seconds
        self.model_name = model_name or getattr(config, "ANDROID_PHONE_AGENT_MODEL", "google/gemini-2.5-flash")
        self.user_id = user_id
        self.int_user_id = int(user_id) if isinstance(user_id, (int, str)) and str(user_id).isdigit() else None
        self.target_package = target_package

        self.step_history: List[Dict[str, Any]] = []
        self.action_history_signatures: List[str] = []
        self.is_completed = False
        self.result_summary: Optional[str] = None
        self.active_task_id: Optional[str] = None
        self.scratchpad_artifacts: Dict[str, Any] = {}

        # Same resume-checkpoint steering shape as LightWebAgent
        # (channels/light_web_agent.py __init__'s own comment explains the
        # full reasoning) -- a human's answer to a just-resumed checkpoint
        # steers the very next planner call, then normal detection resumes.
        self.resume_checkpoint = resume_checkpoint
        self._resume_checkpoint_pending = resume_checkpoint is not None
        if resume_checkpoint is not None and resume_checkpoint.human_input:
            self.credentials.setdefault("checkpoint_answer", resume_checkpoint.human_input)

        self._start_time = time.time()

    async def _init_scratchpad(self) -> None:
        if self.int_user_id is None or not getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            return
        try:
            task = await db.get_active_task(self.int_user_id)
            if task is None:
                task = await db.start_active_task(self.int_user_id, task_type="android_phone_agent")
            if task:
                self.active_task_id = task.get("task_id")
                self.scratchpad_artifacts = task.get("artifacts") or {}
        except Exception as e:
            logger.debug(f"[AndroidPhoneAgent] scratchpad init fallback: {e}")

    async def _update_scratchpad(self, updates: Dict[str, Any]) -> None:
        if not updates:
            return
        self.scratchpad_artifacts.update(updates)
        if self.active_task_id and getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            try:
                await db.update_active_task_artifacts(self.active_task_id, updates)
            except Exception as e:
                logger.debug(f"[AndroidPhoneAgent] scratchpad update fallback: {e}")

    async def _persist_pending_checkpoint(self, checkpoint: PhoneCheckpoint) -> None:
        """Sibling of LightWebAgent._persist_pending_checkpoint -- same
        ordering requirement (artifacts write BEFORE the status flip to
        'waiting_user_input'; see that method's own docstring for why)."""
        if not self.active_task_id or not getattr(config, "SCRATCHPAD_AND_SKILLS_ENABLED", True):
            return
        try:
            await self._update_scratchpad({
                "pending_checkpoint": {
                    "device_id": self.device_row["id"],
                    "kind": checkpoint.kind,
                    "prompt_to_user": checkpoint.prompt_to_user,
                    "screenshot_share_token": checkpoint.screenshot_share_token,
                    "asked_at": time.time(),
                },
                "_call_context": {
                    "goal": self.user_goal,
                    "credentials": self.credentials,
                    "max_steps": self.max_steps,
                    "timeout_seconds": self.timeout_seconds,
                    "target_package": self.target_package,
                },
            })
            await db.set_active_task_status(self.active_task_id, "waiting_user_input")
        except Exception as e:
            logger.debug(f"[AndroidPhoneAgent] _persist_pending_checkpoint fallback: {e}")
        if self.int_user_id is not None:
            # One spot for all three NEEDS_HUMAN paths (unexpected dialog,
            # explicit needs_help, consequential-action milestone confirm)
            # -- every one of them calls this method, so the live-view
            # page's "waiting on you" state and the milestone screenshot it
            # shows are always in sync with what actually got persisted.
            phone_activity.set_waiting(self.int_user_id, checkpoint.prompt_to_user)
            phone_activity.set_screenshot_token(self.int_user_id, checkpoint.screenshot_share_token)

    def _check_circuit_breakers(self) -> Optional[PhoneAgentDecision]:
        """Step cap + oscillation detector -- same period-1/2/3 scan over
        the last 6 signatures as light_web_agent.py's
        _check_circuit_breakers (see that method's own comment for why
        period 1-3, not just a hardcoded period-2 window)."""
        if len(self.step_history) >= self.max_steps:
            return PhoneAgentDecision(
                status="FAILED",
                thought="Reached the maximum step limit for this task.",
                result_summary=f"Stopped after {self.max_steps} steps without finishing '{self.user_goal}'.",
            )
        if time.time() - self._start_time > self.timeout_seconds:
            return PhoneAgentDecision(
                status="FAILED",
                thought="Reached the time limit for this task.",
                result_summary=f"Stopped after {self.timeout_seconds}s without finishing '{self.user_goal}'.",
            )
        sigs = self.action_history_signatures[-6:]
        for period in (1, 2, 3):
            if len(sigs) >= period * 2 and sigs[-period:] == sigs[-2 * period:-period]:
                return PhoneAgentDecision(
                    status="FAILED",
                    thought=f"Detected a repeating {period}-step loop -- not making progress.",
                    result_summary=(
                        f"Stopped: the last {period} action(s) repeated without the screen changing. "
                        "This app may need a different approach."
                    ),
                )
        return None

    async def _sense(self) -> tuple[List[Dict[str, Any]], str, str, str]:
        """Returns (elements, prompt_text, current_package, current_activity)."""
        xml_str = await asyncio.to_thread(self.device.dump_hierarchy)
        elements, prompt_text = extract_pruned_hierarchy(xml_str)
        try:
            app_info = await asyncio.to_thread(self.device.app_current)
            current_package = app_info.get("package", "") if isinstance(app_info, dict) else ""
            current_activity = app_info.get("activity", "") if isinstance(app_info, dict) else ""
        except Exception:
            current_package, current_activity = "", ""
        return elements, prompt_text, current_package, current_activity

    async def _capture_milestone_screenshot(self) -> Optional[str]:
        """Stores the current screen as a Postgres-backed document share
        (same mechanism stagehand_tools.py's _send_screenshot uses) and
        returns its share token, or None if unavailable -- a failure here
        must never block the checkpoint itself from being raised."""
        if self.int_user_id is None:
            return None
        try:
            img_bytes = await asyncio.to_thread(self.device.screenshot, None)
            import io
            buf = io.BytesIO()
            img_bytes.save(buf, format="PNG")
            raw = buf.getvalue()
        except Exception as e:
            logger.debug(f"[AndroidPhoneAgent] milestone screenshot capture failed: {e}")
            return None
        if len(raw) > getattr(config, "MAX_SMS_ATTACHMENT_BYTES", 8 * 1024 * 1024):
            return None
        import uuid as _uuid
        filename = f"android_milestone_{_uuid.uuid4().hex[:12]}.png"
        try:
            token = await db.create_document_share(
                self.int_user_id,
                f"{getattr(config, 'OUTPUTS_DIR', '/tmp').rstrip('/')}/{filename}",
                filename,
                file_bytes=raw,
                media_type="image/png",
            )
        except Exception as e:
            logger.debug(f"[AndroidPhoneAgent] create_document_share failed: {e}")
            return None
        return token

    async def _execute_macro_action(self, action: PhoneMacroAction, elem_map: Dict[str, Any]) -> tuple[bool, Optional[str]]:
        """Executes a batch of PhoneSteps in order, stopping at the first
        failure. Token substitution ({{cred:...}}) matches light_web_
        agent.py's own convention exactly, so credentials/checkpoint
        answers move through both engines the same way."""
        for step in action.steps:
            try:
                val = step.value
                if val and "{{cred:" in val:
                    for cred_key, cred_val in self.credentials.items():
                        val = val.replace(f"{{{{cred:{cred_key}}}}}", cred_val)

                if step.action == "app_start":
                    if not val:
                        return False, "app_start requires a package name in 'value'."
                    await asyncio.to_thread(self.device.app_start, val)
                    await asyncio.sleep(1.5)
                elif step.action == "press_key":
                    if not val:
                        return False, "press_key requires a key name in 'value'."
                    await asyncio.to_thread(self.device.press, val)
                elif step.action == "wait_for":
                    await asyncio.sleep(min(float(val) if val else 1.0, 10.0))
                elif step.action == "swipe":
                    direction = (val or "up").strip().lower()
                    w, h = await asyncio.to_thread(lambda: self.device.window_size())
                    cx, cy = w // 2, h // 2
                    deltas = {
                        "up": (cx, int(h * 0.75), cx, int(h * 0.25)),
                        "down": (cx, int(h * 0.25), cx, int(h * 0.75)),
                        "left": (int(w * 0.75), cy, int(w * 0.25), cy),
                        "right": (int(w * 0.25), cy, int(w * 0.75), cy),
                    }
                    sx, sy, ex, ey = deltas.get(direction, deltas["up"])
                    await asyncio.to_thread(self.device.swipe, sx, sy, ex, ey, 0.3)
                elif step.action in ("tap", "type"):
                    elem = elem_map.get(step.target_id) if step.target_id else None
                    if elem is None:
                        return False, f"Target element [{step.target_id}] no longer exists on screen."
                    if step.target_signature and elem.get("signature") != step.target_signature:
                        return False, (
                            f"Target [{step.target_id}] changed since it was chosen (stale screen) -- "
                            "re-sensing is needed."
                        )
                    cx, cy = elem["center"]
                    if step.action == "tap":
                        await asyncio.to_thread(self.device.click, cx, cy)
                    else:  # type
                        await asyncio.to_thread(self.device.click, cx, cy)
                        await asyncio.sleep(0.2)
                        await asyncio.to_thread(self.device.clear_text)
                        await asyncio.to_thread(self.device.send_keys, val or "")
                else:
                    return False, f"Unknown action '{step.action}'."
                await asyncio.sleep(0.4)  # brief settle time between steps, same spirit as light_web_agent's stabilization wait
            except Exception as e:
                code_name, guidance = translate_device_error(e)
                logger.warning(f"[AndroidPhoneAgent] step failed ({code_name}): {e}")
                return False, guidance
        return True, None

    async def _query_llm_planner(
        self, prompt_text: str, current_package: str, current_activity: str, elem_map: Dict[str, Any],
    ) -> PhoneAgentDecision:
        history_lines = []
        for idx, h in enumerate(self.step_history[-4:], 1):
            steps_desc = ", ".join(
                f"{s.get('action')} on {s.get('target_id')}" for s in h.get("action", {}).get("steps", [])
            ) or h.get("type", "action")
            history_lines.append(f"- Step {idx}: {steps_desc} (success={h.get('success', True)})")
        history_str = "\n".join(history_lines) if history_lines else "None yet."

        resume_directive = ""
        if self.resume_checkpoint is not None and self._resume_checkpoint_pending:
            ck = self.resume_checkpoint
            resume_directive = (
                f"\n\n[RESUMED FROM HUMAN CHECKPOINT]: A human was just asked '{ck.prompt_to_user}' and "
                "has now answered. Their answer is available as the token '{{cred:checkpoint_answer}}' -- "
                "do NOT ask again. Find the field/button on THIS screen that corresponds to that "
                "checkpoint and act on it now using that token."
            )
            self._resume_checkpoint_pending = False

        system_prompt = (
            "You are Messa's Open-Source Phone agent, driving a real Android phone through its actual "
            "UI to accomplish the user's goal, the way a human would tap through the app.\n\n"
            "Rules:\n"
            "1. Output ONLY valid JSON matching the schema below.\n"
            "2. Batch up to 4 steps when the next few taps are obvious and sequential (e.g. tap a field, "
            "type into it, tap submit).\n"
            "3. Use exact element ids [eX] from the screen listing for 'tap'/'type' targets.\n"
            "4. For passwords/sensitive values use tokens: '{{cred:password}}', '{{cred:email}}'.\n"
            "5. If the goal is fully achieved, output status 'DONE' with a concise result_summary.\n"
            "6. Set is_consequential=true for anything that spends money, submits a final order, deletes "
            "something, or changes an account/security setting -- this pauses for human confirmation "
            "first, so use it generously for anything real-world-consequential.\n"
            "7. If the screen doesn't match what you expected (an unfamiliar dialog, a blocked path), "
            "don't force through it -- try dismissing it or ask for help via NEEDS_HUMAN.\n\n"
            "Response schema:\n"
            '{"status": "OK_REASONED", "thought": "...", "action": {"steps": [{"action": '
            '"tap|type|press_key|swipe|app_start|wait_for", "target_id": "e1", "value": "..."}], '
            '"is_consequential": false}}\n'
            'or {"status": "DONE", "thought": "...", "result_summary": "..."}\n'
            'or {"status": "NEEDS_HUMAN", "thought": "...", "checkpoint": {"kind": '
            '"unexpected_dialog|needs_help", "prompt_to_user": "..."}}'
        )
        user_content = (
            f"User Goal: {self.user_goal}\n"
            f"Current app package: {current_package or 'unknown'} (activity: {current_activity or 'unknown'})\n"
            f"Target app package: {self.target_package or '(not specified -- infer from the goal)'}\n\n"
            f"<screen_contents_untrusted>\n{prompt_text}\n</screen_contents_untrusted>\n\n"
            f"Recent Steps Taken:\n{history_str}"
            f"{resume_directive}\n\n"
            f"Total steps so far: {len(self.step_history)}\n"
            "Output your next decision in JSON."
        )

        try:
            model = config.build_model(self.model_name, api_key=config.api_key_for_agent("android_phone_agent"))
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
            resp = await model.ainvoke(messages)
            raw = resp.content if isinstance(resp.content, str) else str(resp.content)
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if not match:
                return PhoneAgentDecision(status="FAILED", result_summary="Planner returned no parseable JSON.")
            data = json.loads(match.group(0))
            action_data = data.get("action")
            action = PhoneMacroAction(**action_data) if action_data else None
            checkpoint_data = data.get("checkpoint")
            checkpoint = PhoneCheckpoint(**checkpoint_data) if checkpoint_data else None
            return PhoneAgentDecision(
                status=data.get("status", "FAILED"),
                thought=data.get("thought"),
                action=action,
                checkpoint=checkpoint,
                result_summary=data.get("result_summary"),
                scratchpad_updates=data.get("scratchpad_updates"),
            )
        except Exception as e:
            logger.error(f"[AndroidPhoneAgent] planner call failed: {e}")
            return PhoneAgentDecision(status="FAILED", result_summary=f"Planner error: {e}")

    async def run(self) -> PhoneAgentDecision:
        await self._init_scratchpad()

        for _step_idx in range(len(self.step_history), self.max_steps):
            breaker_decision = self._check_circuit_breakers()
            if breaker_decision:
                return breaker_decision

            elements, prompt_text, current_package, current_activity = await self._sense()
            elem_map = {el["id"]: el for el in elements}

            dialog_reason = detect_unexpected_dialog(current_package, self.target_package)
            if dialog_reason:
                screenshot_token = await self._capture_milestone_screenshot()
                checkpoint = PhoneCheckpoint(
                    kind="unexpected_dialog",
                    prompt_to_user=(
                        f"{dialog_reason} I've paused so you can tell me how to handle it -- reply with "
                        "what I should do, or \"dismiss it\" if I should just close it and continue."
                    ),
                    screenshot_share_token=screenshot_token,
                    trigger_reason="unexpected_foreground_app",
                )
                await self._persist_pending_checkpoint(checkpoint)
                return PhoneAgentDecision(
                    status="NEEDS_HUMAN",
                    thought=f"Unexpected foreground app/dialog: {current_package}",
                    checkpoint=checkpoint,
                )

            decision = await self._query_llm_planner(prompt_text, current_package, current_activity, elem_map)

            if self.int_user_id is not None:
                # Live-view feed (server.py's GET /live/{token}/phone/status,
                # phone_activity.py): what makes that page feel genuinely
                # live rather than a still screenshot that happens to
                # refresh -- a running "here's what I'm doing" line updated
                # after every planner decision, not just at milestones.
                phone_activity.set_progress(self.int_user_id, decision.thought, len(self.step_history) + 1)

            if decision.scratchpad_updates:
                await self._update_scratchpad(decision.scratchpad_updates)

            if decision.status == "DONE":
                self.is_completed = True
                self.result_summary = decision.result_summary
                if self.active_task_id:
                    try:
                        await db.set_active_task_status(self.active_task_id, "completed")
                    except Exception:
                        pass
                return decision

            if decision.status == "NEEDS_HUMAN":
                checkpoint = decision.checkpoint or PhoneCheckpoint(
                    kind="needs_help", prompt_to_user="I need your help to continue this task."
                )
                if not checkpoint.screenshot_share_token:
                    checkpoint.screenshot_share_token = await self._capture_milestone_screenshot()
                await self._persist_pending_checkpoint(checkpoint)
                return PhoneAgentDecision(status="NEEDS_HUMAN", thought=decision.thought, checkpoint=checkpoint)

            if decision.status == "FAILED" or not decision.action:
                return decision

            if decision.action.is_consequential:
                screenshot_token = await self._capture_milestone_screenshot()
                summary = decision.thought or "a consequential action"
                checkpoint = PhoneCheckpoint(
                    kind="milestone_confirm",
                    prompt_to_user=(
                        f"About to do this on your phone: {summary}. Reply YES to continue or NO to stop."
                    ),
                    screenshot_share_token=screenshot_token,
                    trigger_reason="consequential_action",
                )
                await self._persist_pending_checkpoint(checkpoint)
                return PhoneAgentDecision(
                    status="NEEDS_HUMAN",
                    thought=f"Consequential action awaiting confirmation: {summary}",
                    checkpoint=checkpoint,
                )

            exec_ok, err_msg = await self._execute_macro_action(decision.action, elem_map)
            sig = hashlib.sha256(
                json.dumps([s.dict() for s in decision.action.steps], sort_keys=True).encode()
            ).hexdigest()[:12]
            self.action_history_signatures.append(sig)
            self.step_history.append({
                "type": "macro_action", "action": decision.action.dict(), "success": exec_ok, "error": err_msg,
            })

        return PhoneAgentDecision(
            status="FAILED",
            result_summary=f"Ran out of steps working on '{self.user_goal}'.",
        )


# ---------------------------------------------------------------------------
# Entry-point orchestration -- connect/run/release, mirrors channels/
# browser.py's run_light_web_task/resume_light_web_task shape (that module
# additionally routes between cloud providers; there's only one "provider"
# for a phone -- the user's own device -- so that layer lives here instead
# of a separate file).
# ---------------------------------------------------------------------------

async def run_android_phone_task(
    goal: str,
    *,
    user_id: Any,
    device_row: Dict[str, Any],
    target_package: Optional[str] = None,
    max_steps: Optional[int] = None,
    timeout_seconds: Optional[int] = None,
    credentials: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Connects to an already-paired device and runs AndroidPhoneAgent
    toward `goal`. Caller (tools/android_phone_tools.py) is responsible
    for the device queue ticket (device_manager.acquire) and plan-tier
    step/timeout caps -- this function just runs the engine against
    whatever limits it's given.

    Registers/unregisters with phone_activity.py around the whole call
    (see that module's own docstring) so the live-view page's "Pause /
    Abort" button has a real asyncio.Task to cancel for as long as this
    function is on the stack, whether it ends in DONE/FAILED/NEEDS_HUMAN
    or an actual cancellation."""
    device_id = device_row["id"]
    int_user_id = int(user_id) if isinstance(user_id, (int, str)) and str(user_id).isdigit() else None
    if int_user_id is not None:
        phone_activity.register(int_user_id, device_id, goal)
    try:
        try:
            u2_device = await device_manager.connect_device(device_row)
        except ConnectionError as e:
            if int_user_id is not None:
                phone_activity.set_status(int_user_id, "failed")
            return {"status": "FAILED", "result_summary": str(e)}

        agent = AndroidPhoneAgent(
            u2_device=u2_device,
            device_row=device_row,
            user_goal=goal,
            credentials=credentials,
            max_steps=max_steps or 10,
            timeout_seconds=timeout_seconds or 180,
            user_id=user_id,
            target_package=target_package,
        )
        try:
            decision = await agent.run()
        except asyncio.CancelledError:
            # The live-view page's break-glass Pause/Abort button
            # (phone_activity.cancel -> this task.cancel()) -- server.py's
            # abort route already presses the Android Home key directly
            # against the device before cancelling, so all that's left
            # here is releasing the queue lock/DB status and recording the
            # end state before re-raising (this function must not swallow
            # the cancellation -- its own caller's `async with ticket:`
            # block still needs to see it to release cleanly).
            if int_user_id is not None:
                phone_activity.set_status(int_user_id, "aborted")
            device_manager.release(device_id)
            try:
                await db.update_device_status(device_id, "connected")
            except Exception:
                pass
            raise

        if decision.status != "NEEDS_HUMAN":
            device_manager.release(device_id)
            try:
                await db.update_device_status(device_id, "connected")
            except Exception:
                pass
            if int_user_id is not None:
                phone_activity.set_status(int_user_id, "done" if decision.status == "DONE" else "failed")

        result = decision.dict()
        result["steps_taken"] = len(agent.step_history)
        result["device_id"] = device_id
        return result
    finally:
        if int_user_id is not None:
            phone_activity.unregister(int_user_id)


async def resume_android_phone_task(user_id: Any, human_input: str) -> Dict[str, Any]:
    """Resumes a phone task suspended on a PhoneCheckpoint -- sibling of
    channels/browser.py's resume_light_web_task, same reasoning: reads
    back what db.get_pending_android_checkpoint persisted, reconnects (the
    device connection may well still be live in `device_manager` since,
    unlike a cloud browser session, nothing ever explicitly tore it down
    on NEEDS_HUMAN -- connect_device's own "reuse if still connected"
    fast path handles that), and re-runs the engine with resume_checkpoint
    set so it steers the very next planner call at the human's answer
    instead of re-asking the same question."""
    int_user_id = int(user_id) if isinstance(user_id, (int, str)) and str(user_id).isdigit() else None
    if int_user_id is None:
        return {"status": "FAILED", "result_summary": "resume_android_phone_task requires a real user_id."}

    task = await db.get_pending_android_checkpoint(int_user_id)
    if not task:
        return {
            "status": "FAILED",
            "result_summary": "No phone task is currently waiting on a human checkpoint.",
        }

    artifacts = task.get("artifacts") or {}
    pending = artifacts.get("pending_checkpoint") or {}
    call_context = artifacts.get("_call_context") or {}
    device_id = pending.get("device_id")
    device_row = await db.get_device_by_id(device_id) if device_id else None
    if not device_row:
        try:
            await db.set_active_task_status(task["task_id"], "failed")
        except Exception:
            pass
        return {"status": "FAILED", "result_summary": "That phone is no longer paired."}

    try:
        u2_device = await device_manager.connect_device(device_row)
    except ConnectionError as e:
        return {"status": "FAILED", "result_summary": str(e)}

    checkpoint = PhoneCheckpoint(
        kind=pending.get("kind", "needs_help"),
        prompt_to_user=pending.get("prompt_to_user", ""),
        trigger_reason="resumed_from_persisted_checkpoint",
        human_input=human_input,
    )
    credentials = dict(call_context.get("credentials") or {})
    credentials["checkpoint_answer"] = human_input

    agent = AndroidPhoneAgent(
        u2_device=u2_device,
        device_row=device_row,
        user_goal=call_context.get("goal") or "",
        credentials=credentials,
        max_steps=call_context.get("max_steps", 10),
        timeout_seconds=call_context.get("timeout_seconds", 180),
        user_id=user_id,
        target_package=call_context.get("target_package"),
        resume_checkpoint=checkpoint,
    )
    agent.active_task_id = task.get("task_id")
    agent.scratchpad_artifacts = artifacts

    try:
        ticket = await device_manager.acquire(device_id)
    except Exception as e:
        return {"status": "FAILED", "result_summary": str(e)}

    # Same phone_activity register/finally-unregister bracket as
    # run_android_phone_task above (see that function's own comment) --
    # a resumed task is just as cancellable via the live-view page's
    # Pause/Abort button as a fresh one.
    phone_activity.register(int_user_id, device_id, call_context.get("goal") or "")
    try:
        async with ticket:
            try:
                decision = await agent.run()
            except asyncio.CancelledError:
                phone_activity.set_status(int_user_id, "aborted")
                device_manager.release(device_id)
                try:
                    await db.update_device_status(device_id, "connected")
                except Exception:
                    pass
                raise

            if decision.status != "NEEDS_HUMAN":
                device_manager.release(device_id)
                try:
                    await db.update_device_status(device_id, "connected")
                except Exception:
                    pass
                phone_activity.set_status(int_user_id, "done" if decision.status == "DONE" else "failed")

            result = decision.dict()
            result["steps_taken"] = len(agent.step_history)
            result["device_id"] = device_id
            return result
    finally:
        phone_activity.unregister(int_user_id)
