"""Messa Companion APK reverse-tunnel bridge engine (open-source-phone.md
section 2/3, feature/messa-companion-apk).

Second transport for a paired Android phone, alongside the existing
direct-LAN path in messa/devices/android.py (migration 043's
tunnel_host/tunnel_port, still valid whenever the server and the phone
genuinely share a reachable network). This module exists for the case
that motivated the whole feature: Messa's server usually can NOT dial
into the phone at all (Hugging Face Spaces is HTTP/WebSocket-only, no raw
inbound UDP/TCP; a home router's NAT/CGNAT blocks unsolicited inbound
connections regardless). So instead of the server dialing in, the Messa
Companion APK's own persistent foreground service dials OUT to this
server over a normal outbound `wss://.../device/ws` connection -- which
works through any router, any NAT, any CGNAT, because the phone always
initiates.

What this module is NOT: it does not speak ADB. It is a pure byte-pipe,
the same shape as a reverse ngrok/SSH tunnel -- CompanionDeviceBridge
opens a local loopback TCP socket that adbutils/uiautomator2 connect to
exactly as they would a real `adb connect 127.0.0.1:<port>` endpoint, and
relays whatever bytes cross that socket to/from the WebSocket unmodified.
All of the actual ADB protocol handling stays in adbutils, completely
unaware it isn't talking to a phone on the same LAN.

Three layers, matching how a `/device/ws` connection actually progresses:

  1. Pairing (CompanionBridgeManager.start_pairing / bind_pairing_code) --
     a fresh APK install has a device secret but no user_devices row yet.
     It connects, gets a 6-digit code back, displays it, and waits; the
     user texts that code from their registered number (open-source-
     phone.md section 3.2's single-tenant SMS handshake), which is what
     actually creates the row and binds this device to that one user --
     never just "whoever connects first."
  2. Live bridging (CompanionDeviceBridge / start_device_bridge) -- once
     bound (or on every subsequent reconnect of an already-paired phone),
     the connection becomes a routable byte-pipe that messa/devices/
     android.py's connect_device looks up by device_id.
  3. Heartbeat/staleness -- both sides ping every
     config.COMPANION_BRIDGE_HEARTBEAT_SECONDS (25s, comfortably under the
     30-60s idle-WebSocket timeout open-source-phone.md section 4
     documents for Cloudflare/HF's proxy layer), and a bridge that's heard
     nothing in config.COMPANION_BRIDGE_STALE_TIMEOUT_SECONDS tears itself
     down rather than leaving adbutils hung against a socket nobody will
     ever answer again.

Process-wide singleton (MANAGER below), never persisted to Postgres --
same "can't survive a restart anyway" reasoning phone_activity.py,
call_activity.py, and deepsearch_control.py already document for their
own in-memory-only state. A pending pairing code is scoped to exactly one
live WebSocket that hasn't been claimed by an SMS yet; a live bridge's
local_port is an OS-assigned ephemeral port meaningful only to this one
process's currently-running listener. Migration 044 deliberately does not
add a table for either.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Secrets & codes
# ---------------------------------------------------------------------------

def hash_device_secret(device_secret: str) -> str:
    """sha256 hex digest -- the only form of the APK's device secret that
    is ever persisted (migration 044's device_secret_hash column). Both
    the pairing path and the per-connection auth path route through this
    one function so there is exactly one place the hashing algorithm is
    chosen, never duplicated between them."""
    return hashlib.sha256(device_secret.encode("utf-8")).hexdigest()


def generate_device_secret() -> str:
    """256-bit random token, hex-encoded (64 chars). The real Companion
    APK generates its own client-side on first launch and never sends
    this function's output anywhere -- this exists for tests and any
    server-side tooling that needs to simulate a device."""
    return secrets.token_hex(32)


def generate_pairing_code() -> str:
    """6-digit pairing code formatted like open-source-phone.md's own
    example ('PAIR 918-243'): zero-padded so it's always exactly 6
    digits, grouped 3-3 with a dash purely for human readability when
    the user is copying it into a text message."""
    n = secrets.randbelow(1_000_000)
    digits = f"{n:06d}"
    return f"{digits[:3]}-{digits[3:]}"


def _normalize_code(code: str) -> str:
    """Strips everything but digits so 'PAIR 918-243', '918-243', and
    '918243' all compare equal -- a user copying the code out of a text
    field shouldn't need to match punctuation exactly."""
    return "".join(ch for ch in code if ch.isdigit())


# ---------------------------------------------------------------------------
# Pairing state
# ---------------------------------------------------------------------------

@dataclass
class _PendingPairing:
    """One in-flight 'APK is showing a code, waiting for the matching SMS'
    session. Exists only for the handful of seconds/minutes between the
    APK connecting and the user texting the code back -- see this
    module's own header for why nothing here is persisted."""

    code: str
    device_secret: str
    device_secret_hash: str
    device_name: str
    websocket: Any
    created_at: float = field(default_factory=time.time)
    bound_future: "asyncio.Future[Optional[dict]]" = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    ping_task: Optional[asyncio.Task] = None

    def is_expired(self, ttl_seconds: int) -> bool:
        return (time.time() - self.created_at) > ttl_seconds


class CompanionBridgeManager:
    """Process-wide singleton (module-level MANAGER below). Tracks pending
    pairing codes waiting to be claimed by an SMS, per-phone-number failed
    pairing attempts (anti-bruteforce), and live device bridges keyed by
    device_id. Single-threaded asyncio event loop means plain dict
    mutation here needs no extra locking -- nothing below awaits between
    reading and writing the same entry."""

    def __init__(self) -> None:
        self._pending: dict[str, _PendingPairing] = {}
        self._attempts: dict[str, tuple[int, float]] = {}  # phone_number -> (failed_count, window_started_at)
        self._bridges: dict[int, "CompanionDeviceBridge"] = {}

    # ---- Pairing: APK side ----

    def start_pairing(self, websocket: Any, device_secret: str, device_name: str) -> _PendingPairing:
        """Called the instant a not-yet-bound `/device/ws` connection
        authenticates with a device secret that doesn't match any
        existing user_devices row. Generates a fresh code, stashes the
        pending session (including a Future the route awaits until an SMS
        binds it, expires, or the socket disconnects), and returns it so
        the route can send the code back down the socket."""
        self._prune_expired()
        code = generate_pairing_code()
        while code in self._pending:  # practically never collides at 10^6 codes, but stay correct
            code = generate_pairing_code()
        pending = _PendingPairing(
            code=code,
            device_secret=device_secret,
            device_secret_hash=hash_device_secret(device_secret),
            device_name=device_name,
            websocket=websocket,
        )
        self._pending[code] = pending
        return pending

    def _prune_expired(self) -> None:
        ttl = config.COMPANION_PAIRING_CODE_TTL_SECONDS
        expired = [c for c, p in self._pending.items() if p.is_expired(ttl)]
        for c in expired:
            pending = self._pending.pop(c)
            if not pending.bound_future.done():
                pending.bound_future.set_result(None)
            if pending.ping_task is not None:
                pending.ping_task.cancel()

    def cancel_pairing_for_socket(self, websocket: Any) -> None:
        """Called when a not-yet-bound WS disconnects before its code was
        ever claimed (APK closed, network drop). Without this the code
        would sit in _pending, technically still claimable by an SMS,
        against a socket that's already gone."""
        stale = [c for c, p in self._pending.items() if p.websocket is websocket]
        for c in stale:
            pending = self._pending.pop(c)
            if not pending.bound_future.done():
                pending.bound_future.set_result(None)
            if pending.ping_task is not None:
                pending.ping_task.cancel()

    # ---- Pairing: SMS side ----

    def _attempts_allowed(self, phone_number: str) -> bool:
        self._prune_attempt_window(phone_number)
        count, _ = self._attempts.get(phone_number, (0, time.time()))
        return count < config.COMPANION_PAIRING_MAX_ATTEMPTS

    def _prune_attempt_window(self, phone_number: str) -> None:
        entry = self._attempts.get(phone_number)
        if entry is None:
            return
        _, window_started_at = entry
        if (time.time() - window_started_at) > config.COMPANION_PAIRING_CODE_TTL_SECONDS:
            del self._attempts[phone_number]

    def _record_failed_attempt(self, phone_number: str) -> None:
        self._prune_attempt_window(phone_number)
        count, window_started_at = self._attempts.get(phone_number, (0, time.time()))
        self._attempts[phone_number] = (count + 1, window_started_at)

    def _clear_attempts(self, phone_number: str) -> None:
        self._attempts.pop(phone_number, None)

    async def bind_pairing_code(
        self, code: str, phone_number: str, user_id: int,
    ) -> Optional[dict]:
        """Called from server.py's SMS-webhook short-circuit the moment an
        inbound text looks like a pairing code. Cap-checks the SENDING
        number first (open-source-phone.md section 3.2's 'max 3 attempts'
        -- scoped per-number since a wrong code has no natural 'which
        pending session did you mean' match), then resolves the matching
        pending session and creates/updates the user_devices row via
        db.bind_companion_device_secret. This is the ONE place a
        device_secret_hash is ever written, deliberately gated behind an
        SMS from the phone's OWN registered number, never just 'whoever
        connects first'. Returns the bound device row, or None on any
        failure (locked out, expired/unknown code, or the db write itself
        failing) -- callers should treat every None the same way (a
        generic 'that code didn't work' reply), not distinguish reasons
        back to the SMS sender, so a brute-force attempt learns nothing.
        """
        self._prune_expired()
        if not self._attempts_allowed(phone_number):
            return None

        normalized = _normalize_code(code)
        match_key = None
        for stored_code, pending in self._pending.items():
            if _normalize_code(stored_code) == normalized:
                match_key = stored_code
                break

        if match_key is None:
            self._record_failed_attempt(phone_number)
            return None

        pending = self._pending.pop(match_key)
        if pending.ping_task is not None:
            pending.ping_task.cancel()

        device = await db.bind_companion_device_secret(
            user_id=user_id,
            device_name=pending.device_name,
            device_secret_hash=pending.device_secret_hash,
        )
        if device is None:
            self._record_failed_attempt(phone_number)
            if not pending.bound_future.done():
                pending.bound_future.set_result(None)
            return None

        self._clear_attempts(phone_number)
        if not pending.bound_future.done():
            pending.bound_future.set_result(device)
        return device

    # ---- Auth: existing (already-paired) devices ----

    async def authenticate_device_secret(self, device_secret: str) -> Optional[dict]:
        """Looks up the user_devices row a presented bearer secret belongs
        to, for an APK that's already been paired and is just reconnecting
        (e.g. app restart, network blip). Wraps db.get_device_by_secret_hash
        so callers never touch the hashing algorithm directly."""
        return await db.get_device_by_secret_hash(hash_device_secret(device_secret))

    # ---- Live bridges ----

    def get_bridge(self, device_id: int) -> Optional["CompanionDeviceBridge"]:
        return self._bridges.get(device_id)

    def register_bridge(self, device_id: int, bridge: "CompanionDeviceBridge") -> None:
        self._bridges[device_id] = bridge

    def pop_bridge(self, device_id: int, expected: "CompanionDeviceBridge") -> None:
        # Only remove if it's still THIS bridge -- a newer reconnect may
        # already have replaced it (see start_device_bridge), and an old
        # bridge's own delayed close() must not clobber the new one.
        if self._bridges.get(device_id) is expected:
            del self._bridges[device_id]


MANAGER = CompanionBridgeManager()


# ---------------------------------------------------------------------------
# Live bridge: local loopback socket <-> WebSocket byte pump
# ---------------------------------------------------------------------------

class CompanionDeviceBridge:
    """One physical phone's live companion_ws connection: a local asyncio
    TCP 'virtual ADB loopback socket' on 127.0.0.1, wired byte-for-byte to
    the phone's outbound WebSocket. adbutils/uiautomator2 (messa/devices/
    android.py's connect_device) dial (127.0.0.1, self.local_port) exactly
    as they would a real `adb connect` endpoint -- this class does no ADB
    protocol work at all, it is a pure byte-pipe. local_port is an
    OS-assigned ephemeral port, deliberately never persisted (see this
    module's header) -- it's only meaningful for as long as this exact
    process and this exact WebSocket connection are both alive.

    Only one local TCP connection is expected at a time -- that mirrors
    the real constraint it stands in for (a single physical phone only
    ever has one ADB daemon to talk to) -- but a local connection ending
    (adbutils finished one task's work and disconnected) does NOT tear
    down the bridge itself: the phone's Companion APK holds its WebSocket
    open across many tasks (that's the whole point of a persistent
    foreground service), so the WS-read loop below runs for the bridge's
    entire lifetime, independent of whether a local ADB client happens to
    be attached at this exact moment. Only the WebSocket itself going away
    (or going stale) ends the bridge."""

    _PENDING_WS_BUFFER_CAP = 200  # small safety cap, see _pump_ws_to_local

    def __init__(
        self,
        device_id: int,
        websocket: Any,
        on_touch_abort: Optional[Any] = None,
    ) -> None:
        self.device_id = device_id
        self.websocket = websocket
        # Optional async callback (device_id: int) -> None, invoked when
        # the phone's touch-killswitch overlay (open-source-phone.md
        # section 3.4) sends a `{"type": "touch_abort"}` control message
        # -- wired up by server.py's /device/ws route to
        # _press_home_and_abort_phone_task. Kept optional/loosely typed so
        # this module has no import-time dependency on server.py or on
        # whatever resolves device_id -> user_id.
        self._on_touch_abort = on_touch_abort
        self.local_port: Optional[int] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._tcp_writer: Optional[asyncio.StreamWriter] = None
        self._closed = False
        self._closed_event = asyncio.Event()
        self._last_activity = time.time()
        self._local_pump_task: Optional[asyncio.Task] = None
        self._ws_pump_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        # Bytes that arrived from the WebSocket before any local ADB client
        # had connected yet (a benign race -- the phone side and the
        # local-connect side start independently). Flushed to the next
        # local connection the moment one attaches; capped so a
        # misbehaving/never-connecting local side can't grow this forever.
        self._pending_ws_bytes: list[bytes] = []

    async def start(self) -> int:
        """Opens the local loopback listener on an OS-assigned ephemeral
        port, and starts BOTH the WebSocket-read pump and the heartbeat --
        both run for the bridge's whole lifetime, not scoped to any one
        local ADB connection. Returns the assigned port."""
        self._server = await asyncio.start_server(
            self._handle_local_connection, host="127.0.0.1", port=0,
        )
        self.local_port = self._server.sockets[0].getsockname()[1]
        self._ws_pump_task = asyncio.create_task(self._pump_ws_to_local())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        return self.local_port

    async def _handle_local_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
    ) -> None:
        """adbutils just connected to our loopback socket. Every byte it
        writes here is forwarded as a binary WebSocket frame for as long
        as THIS local connection lasts; the reverse direction (WS ->
        local) is handled by the bridge-lifetime _pump_ws_to_local task,
        which writes to whichever local connection is current. When this
        local connection ends (adbutils is done with this one task), only
        this local socket is cleaned up -- the WebSocket to the phone
        stays open for the next task."""
        if self._tcp_writer is not None:
            try:
                self._tcp_writer.close()
            except Exception:
                pass
        self._tcp_writer = writer
        if self._pending_ws_bytes:
            try:
                for chunk in self._pending_ws_bytes:
                    writer.write(chunk)
                await writer.drain()
            except Exception:
                pass
            self._pending_ws_bytes.clear()

        self._local_pump_task = asyncio.create_task(self._pump_local_to_ws(reader))
        try:
            await self._local_pump_task
        finally:
            if self._tcp_writer is writer:
                self._tcp_writer = None
            try:
                writer.close()
            except Exception:
                pass

    async def _pump_local_to_ws(self, reader: asyncio.StreamReader) -> None:
        try:
            while not self._closed:
                data = await reader.read(65536)
                if not data:
                    break
                await self.websocket.send_bytes(data)
                self._last_activity = time.time()
        except (asyncio.CancelledError, Exception):
            pass

    async def _pump_ws_to_local(self) -> None:
        """Runs for the whole bridge lifetime (started once in start()).
        The phone side going away (a real disconnect, not just "no local
        ADB client attached right now") is the one thing that legitimately
        ends the entire bridge -- so this is the only pump whose exit
        triggers close()."""
        try:
            while not self._closed:
                message = await self.websocket.receive()
                msg_type = message.get("type")
                if msg_type == "websocket.disconnect":
                    break
                data = message.get("bytes")
                if data is not None:
                    self._last_activity = time.time()
                    if self._tcp_writer is not None:
                        try:
                            self._tcp_writer.write(data)
                            await self._tcp_writer.drain()
                        except Exception:
                            break
                    else:
                        self._pending_ws_bytes.append(data)
                        if len(self._pending_ws_bytes) > self._PENDING_WS_BUFFER_CAP:
                            self._pending_ws_bytes.pop(0)
                    continue
                text = message.get("text")
                if text == "pong":
                    self._last_activity = time.time()
                    continue
                if text:
                    self._last_activity = time.time()
                    await self._handle_text_control_message(text)
        except (asyncio.CancelledError, Exception):
            pass
        finally:
            await self.close()

    async def _handle_text_control_message(self, text: str) -> None:
        """Parses a JSON control frame from the APK. The one message this
        currently handles is the touch-killswitch (open-source-phone.md
        section 3.4): `{"type": "touch_abort"}`, sent the instant the
        phone's overlay detects a genuine physical touch while Messa is
        mid-task. Anything unrecognized/unparseable is ignored -- a
        malformed control message must never be able to crash the bridge
        or be mistaken for ADB payload bytes (those only ever arrive as
        binary frames, never text, so there's no ambiguity to worry
        about)."""
        import json

        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(parsed, dict):
            return
        if parsed.get("type") == "touch_abort" and self._on_touch_abort is not None:
            try:
                await self._on_touch_abort(self.device_id)
            except Exception as e:  # noqa: BLE001 - a killswitch callback failing must not crash the bridge
                logger.warning("companion_bridge: touch_abort callback failed for device %s: %s", self.device_id, e)

    async def _heartbeat_loop(self) -> None:
        """25s ping (config.COMPANION_BRIDGE_HEARTBEAT_SECONDS) so
        Cloudflare/HF's idle-WebSocket timeout never fires on a
        genuinely-alive but momentarily-quiet bridge (open-source-
        phone.md section 4). Also the staleness check: if nothing -- not
        even a pong -- has been seen in
        config.COMPANION_BRIDGE_STALE_TIMEOUT_SECONDS, the phone is
        treated as gone and the bridge tears itself down rather than
        leaving adbutils hung against a socket nobody will ever answer
        again."""
        try:
            while not self._closed:
                await asyncio.sleep(config.COMPANION_BRIDGE_HEARTBEAT_SECONDS)
                if self._closed:
                    break
                if (time.time() - self._last_activity) > config.COMPANION_BRIDGE_STALE_TIMEOUT_SECONDS:
                    logger.warning("companion_bridge: device %s stale, closing bridge", self.device_id)
                    await self.close()
                    break
                try:
                    await self.websocket.send_text("ping")
                except Exception:
                    await self.close()
                    break
        except asyncio.CancelledError:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()
        current = asyncio.current_task()
        for task in (self._local_pump_task, self._ws_pump_task, self._heartbeat_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        if self._tcp_writer is not None:
            try:
                self._tcp_writer.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
        MANAGER.pop_bridge(self.device_id, self)
        try:
            from .devices.android import device_manager
            managed = device_manager.get_managed(self.device_id)
            if managed and managed.tunnel_port == self.local_port:
                managed.status = "offline"
                managed.u2_device = None
        except Exception:
            pass

    async def wait_closed(self) -> None:
        await self._closed_event.wait()


async def start_device_bridge(
    device_id: int, websocket: Any, on_touch_abort: Optional[Any] = None,
) -> CompanionDeviceBridge:
    """Promotes a freshly-authenticated (or freshly-bound) `/device/ws`
    connection into a live, routable bridge: opens the local loopback
    listener, registers it in MANAGER so devices/android.py's
    connect_device can find it by device_id, and starts the heartbeat.
    Any PRIOR bridge for this same device_id (the APK reconnected after a
    network blip before the old socket's staleness timeout fired) is
    closed first -- only one live bridge per device makes sense, matching
    the single-ADB-daemon-per-phone reality this stands in for.

    on_touch_abort, if given, is an async callable(device_id) invoked when
    the phone's touch-killswitch overlay signals a physical touch (section
    3.4) -- server.py wires this to the same home-press + task-cancel path
    the live-view page's HTTP abort button already uses."""
    existing = MANAGER.get_bridge(device_id)
    if existing is not None:
        await existing.close()
    bridge = CompanionDeviceBridge(device_id, websocket, on_touch_abort=on_touch_abort)
    await bridge.start()
    MANAGER.register_bridge(device_id, bridge)

    try:
        from .devices.android import device_manager, ManagedDevice
        managed = device_manager.get_managed(device_id)
        if managed is None or managed.tunnel_port != bridge.local_port:
            managed = ManagedDevice(device_id, "127.0.0.1", bridge.local_port)
            device_manager._devices[device_id] = managed
        managed.status = "connected"
        managed.touch()
    except Exception as e:
        logger.debug("Failed to sync managed device state: %s", e)

    return bridge


async def send_pairing_prompt(websocket: Any, pending: _PendingPairing) -> None:
    """Sends the pairing code down to the APK and starts a lightweight
    keepalive ping loop for the duration of the pairing wait -- the
    pairing TTL (default 180s, config.COMPANION_PAIRING_CODE_TTL_SECONDS)
    is longer than Cloudflare/HF's idle-WebSocket timeout (30-60s), so
    without this the connection could be dropped before the user even
    finishes typing the text message."""
    await websocket.send_json({"type": "pairing_required", "code": pending.code})

    async def _ping_loop() -> None:
        try:
            while True:
                await asyncio.sleep(config.COMPANION_BRIDGE_HEARTBEAT_SECONDS)
                await websocket.send_text("ping")
        except (asyncio.CancelledError, Exception):
            pass

    pending.ping_task = asyncio.create_task(_ping_loop())
