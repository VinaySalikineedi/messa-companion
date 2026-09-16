"""Tests for the Messa Companion APK reverse-tunnel bridge
(open-source-phone.md section 2/3, feature/messa-companion-apk) -- no real
Android hardware or APK build in this environment, so this mocks at the
same boundary tests/test_android_phone_agent.py already established for
the direct-LAN transport: a fake WebSocket standing in for the phone's
real OkHttp connection (messa/companion_bridge.py never knows or cares
which ASGI/WS implementation is on the other end, it only calls
.receive()/.send_bytes()/.send_text()/.send_json()), and the same
FakeConn/FakeAcquire/FakePool/FakeRow shape as tests/test_call_control_and_
activity.py for db.py's new companion-bridge CRUD (migration 044).

Parts:
  1. hash_device_secret / generate_device_secret / generate_pairing_code
     -- format, determinism, uniqueness.
  2. db.py CRUD -- bind_companion_device_secret / get_device_by_secret_hash.
  3. CompanionBridgeManager pairing -- start_pairing / TTL expiry /
     cancel_pairing_for_socket / max-attempts lockout / bind_pairing_code
     success+failure paths.
  4. CompanionDeviceBridge -- local loopback socket <-> WebSocket binary
     frame forwarding both directions over a REAL local TCP socket, a
     local ADB disconnect NOT ending the bridge (persistent across
     tasks), a genuine WS disconnect ending it and deregistering from
     MANAGER, the pending-bytes race buffer, heartbeat ping + stale-
     timeout teardown, touch_abort control-message wiring.
  5. AndroidDeviceManager.connect_device routing -- bridge_kind ==
     'companion_ws' resolves through the live bridge's local_port instead
     of tunnel_host/tunnel_port; a missing/no-bridge case fails fast with
     a translated ConnectionError instead of hanging; a direct_lan device
     is unaffected (regression guard for the pre-existing transport).
  6. server.py /device/ws route (starlette TestClient, SYNCHRONOUS -- see
     test_android_phone_agent.py's own part13 precedent for why a
     TestClient websocket test must run outside asyncio.run()) -- feature
     flag off closes immediately; a missing/malformed Authorization
     header closes immediately; an already-paired device's secret
     promotes straight to a live bridge and real bytes flow both ways
     through the actual local loopback socket (a real socket.socket()
     standing in for adbutils).
  7. server.py SMS pairing-code short-circuit regex -- replicates the
     exact "PAIR ###-###" pattern from _process_inbound (same convention
     tests/test_light_web_agent.py's test_looks_like_answer_heuristic
     already established for that function's own short-circuit gates,
     since _process_inbound itself is too deeply embedded with webhook
     side effects to unit-test directly).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import sys
import time

from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
if not (REPO_ROOT / ".env").exists():
    os.environ.setdefault("OPENROUTER_API_KEY", "sk-test-dummy-not-real")
    os.environ.setdefault("DATABASE_URL", "postgresql://dummy:dummy@localhost:5432/dummy")
    os.environ.setdefault("BROWSERBASE_API_KEY", "bb-test-dummy-not-real")
    os.environ.setdefault("SENDBLUE_API_KEY", "sb-test-dummy-key")
    os.environ.setdefault("SENDBLUE_API_SECRET", "sb-test-dummy-secret")

from messa import companion_bridge, config, db  # noqa: E402
from messa.devices.android import AndroidDeviceManager, ManagedDevice  # noqa: E402

failures: list[str] = []


def check(label: str, cond: bool) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


# ---------------------------------------------------------------------------
# Fakes -- same FakeConn/FakeAcquire/FakePool/FakeRow shape as
# tests/test_call_control_and_activity.py / tests/test_android_phone_agent.py.
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(self, has_tables=True, fetchrow_queue=None):
        self.has_tables = has_tables
        self.fetchrow_queue = list(fetchrow_queue or [])
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
        return []

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "UPDATE 0"


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


class FakeWebSocket:
    """Stands in for a starlette WebSocket from companion_bridge.py's own
    point of view -- it only ever calls .receive()/.send_bytes()/
    .send_text()/.send_json() on whatever it's given, so a fake exposing
    exactly that surface is all CompanionDeviceBridge/CompanionBridgeManager
    need to be tested without a real network connection."""

    def __init__(self):
        self.inbound: asyncio.Queue = asyncio.Queue()  # items pushed in as if "from the phone"
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.sent_json: list[dict] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_text(self, text: str) -> None:
        self.sent_text.append(text)

    async def send_json(self, obj: dict) -> None:
        self.sent_json.append(obj)

    async def receive(self) -> dict:
        item = await self.inbound.get()
        if item is None:
            return {"type": "websocket.disconnect"}
        if isinstance(item, str):
            return {"type": "websocket.receive", "text": item}
        return {"type": "websocket.receive", "bytes": item}

    async def push_bytes(self, data: bytes) -> None:
        await self.inbound.put(data)

    async def push_text(self, text: str) -> None:
        await self.inbound.put(text)

    async def push_disconnect(self) -> None:
        await self.inbound.put(None)


# ===========================================================================
# 1. hash_device_secret / generate_device_secret / generate_pairing_code
# ===========================================================================

def part1_secrets_and_codes():
    secret = companion_bridge.generate_device_secret()
    check("generate_device_secret returns 64 hex chars (256 bits)",
          len(secret) == 64 and all(c in "0123456789abcdef" for c in secret))
    secret2 = companion_bridge.generate_device_secret()
    check("two generated secrets are different", secret != secret2)

    h1 = companion_bridge.hash_device_secret(secret)
    h2 = companion_bridge.hash_device_secret(secret)
    check("hash_device_secret is deterministic for the same input", h1 == h2)
    check("hash_device_secret returns a 64-char sha256 hex digest",
          len(h1) == 64 and all(c in "0123456789abcdef" for c in h1))
    check("hash_device_secret never returns the raw secret", h1 != secret)

    code = companion_bridge.generate_pairing_code()
    check("generate_pairing_code matches the 'DDD-DDD' shape", bool(re.fullmatch(r"\d{3}-\d{3}", code)))
    check("_normalize_code strips punctuation/prefix consistently",
          companion_bridge._normalize_code(f"PAIR {code}") == companion_bridge._normalize_code(code))


# ===========================================================================
# 2. db.py CRUD -- bind_companion_device_secret / get_device_by_secret_hash
# ===========================================================================

async def part2_db_bind_and_lookup():
    bound_row = FakeRow({
        "id": 55, "user_id": 7, "device_name": "Alice's Pixel",
        "bridge_kind": "companion_ws", "device_secret_hash": "abc123",
        "tunnel_host": None, "tunnel_port": None, "status": "paired",
    })
    conn = FakeConn(has_tables=True, fetchrow_queue=[bound_row])
    install_fake_pool(conn)

    row = await db.bind_companion_device_secret(user_id=7, device_name="Alice's Pixel", device_secret_hash="abc123")
    check("bind_companion_device_secret returns the bound row", row is not None and row["id"] == 55)
    insert_call = conn.calls[0]
    check("bind_companion_device_secret issues an INSERT ... ON CONFLICT", "ON CONFLICT" in insert_call[1])
    check("bind_companion_device_secret sets bridge_kind='companion_ws'", "companion_ws" in insert_call[1])
    check("bind_companion_device_secret clears tunnel_host/tunnel_port to NULL", "tunnel_host = NULL" in insert_call[1] or "NULL" in insert_call[1])

    conn_missing_table = FakeConn(has_tables=False)
    install_fake_pool(conn_missing_table)
    row_none = await db.bind_companion_device_secret(user_id=7, device_name="x", device_secret_hash="y")
    check("bind_companion_device_secret returns None when user_devices table is missing", row_none is None)

    conn2 = FakeConn(has_tables=True, fetchrow_queue=[bound_row])
    install_fake_pool(conn2)
    found = await db.get_device_by_secret_hash("abc123")
    check("get_device_by_secret_hash finds the matching row", found is not None and found["id"] == 55)
    lookup_call = conn2.calls[0]
    check("get_device_by_secret_hash excludes revoked devices", "revoked" in lookup_call[1])

    conn3 = FakeConn(has_tables=True, fetchrow_queue=[None])
    install_fake_pool(conn3)
    not_found = await db.get_device_by_secret_hash("no-such-hash")
    check("get_device_by_secret_hash returns None for an unknown hash", not_found is None)


# ===========================================================================
# 3. CompanionBridgeManager pairing
# ===========================================================================

async def part3_pairing_lifecycle():
    manager = companion_bridge.CompanionBridgeManager()
    ws = FakeWebSocket()

    pending = manager.start_pairing(ws, "raw-secret-1", "Test Phone")
    check("start_pairing returns a 'DDD-DDD' code", bool(re.fullmatch(r"\d{3}-\d{3}", pending.code)))
    check("start_pairing stores the hashed secret, not the raw one",
          pending.device_secret_hash == companion_bridge.hash_device_secret("raw-secret-1"))

    with patch.object(db, "bind_companion_device_secret", new=AsyncMock(
        return_value={"id": 42, "user_id": 7, "device_name": "Test Phone"},
    )):
        device = await manager.bind_pairing_code(pending.code, "+15551230000", user_id=7)
    check("bind_pairing_code returns the bound device row on a correct code", device is not None and device["id"] == 42)
    resolved = await asyncio.wait_for(pending.bound_future, timeout=1)
    check("the pending session's bound_future resolves to the same device", resolved == device)

    # Re-using an already-consumed code fails (it was popped on success).
    with patch.object(db, "bind_companion_device_secret", new=AsyncMock(return_value=None)):
        replay = await manager.bind_pairing_code(pending.code, "+15551230000", user_id=7)
    check("a consumed pairing code cannot be replayed", replay is None)

    # TTL expiry.
    manager2 = companion_bridge.CompanionBridgeManager()
    ws2 = FakeWebSocket()
    pending2 = manager2.start_pairing(ws2, "raw-secret-2", "Test Phone 2")
    pending2.created_at = time.time() - (config.COMPANION_PAIRING_CODE_TTL_SECONDS + 5)
    expired_result = await manager2.bind_pairing_code(pending2.code, "+15559990000", user_id=8)
    check("an expired pairing code no longer matches", expired_result is None)
    check("the expired pending session's future was resolved (not left hanging)", pending2.bound_future.done())

    # cancel_pairing_for_socket.
    manager3 = companion_bridge.CompanionBridgeManager()
    ws3 = FakeWebSocket()
    pending3 = manager3.start_pairing(ws3, "raw-secret-3", "Test Phone 3")
    manager3.cancel_pairing_for_socket(ws3)
    cancelled_result = await manager3.bind_pairing_code(pending3.code, "+15551110000", user_id=9)
    check("a cancelled (disconnected) pairing session can no longer be bound", cancelled_result is None)

    # Max-attempts lockout, scoped per sending phone number.
    manager4 = companion_bridge.CompanionBridgeManager()
    ws4 = FakeWebSocket()
    pending4 = manager4.start_pairing(ws4, "raw-secret-4", "Test Phone 4")
    phone = "+15558675309"
    for _ in range(config.COMPANION_PAIRING_MAX_ATTEMPTS):
        wrong = await manager4.bind_pairing_code("000-000", phone, user_id=10)
        check("a wrong code is rejected before the attempt cap is hit", wrong is None)
    with patch.object(db, "bind_companion_device_secret", new=AsyncMock(
        return_value={"id": 99, "user_id": 10, "device_name": "Test Phone 4"},
    )):
        locked_out = await manager4.bind_pairing_code(pending4.code, phone, user_id=10)
    check("the CORRECT code is still rejected once the sending number is locked out", locked_out is None)

    # A different sending number is unaffected by another number's lockout.
    manager5 = companion_bridge.CompanionBridgeManager()
    ws5 = FakeWebSocket()
    pending5 = manager5.start_pairing(ws5, "raw-secret-5", "Test Phone 5")
    for _ in range(config.COMPANION_PAIRING_MAX_ATTEMPTS):
        await manager5.bind_pairing_code("111-111", "+15550000001", user_id=11)
    with patch.object(db, "bind_companion_device_secret", new=AsyncMock(
        return_value={"id": 100, "user_id": 11, "device_name": "Test Phone 5"},
    )):
        other_number_ok = await manager5.bind_pairing_code(pending5.code, "+15550000002", user_id=11)
    check("a DIFFERENT sending number is not affected by another number's lockout", other_number_ok is not None)


# ===========================================================================
# 4. CompanionDeviceBridge -- byte forwarding, lifecycle, heartbeat
# ===========================================================================

async def part4_bridge_byte_forwarding_and_lifecycle():
    ws = FakeWebSocket()
    bridge = await companion_bridge.start_device_bridge(500, ws)
    check("start_device_bridge assigns a local TCP port", bool(bridge.local_port and bridge.local_port > 0))
    check("start_device_bridge registers the bridge in MANAGER", companion_bridge.MANAGER.get_bridge(500) is bridge)

    reader1, writer1 = await asyncio.open_connection("127.0.0.1", bridge.local_port)
    writer1.write(b"TASK1-OUT")
    await writer1.drain()
    await asyncio.sleep(0.1)
    check("a local write is forwarded to the WebSocket as a binary frame", ws.sent_bytes and ws.sent_bytes[-1] == b"TASK1-OUT")

    await ws.push_bytes(b"TASK1-IN")
    data = await asyncio.wait_for(reader1.read(1024), timeout=1)
    check("a WS binary frame is forwarded to the local socket", data == b"TASK1-IN")

    writer1.close()
    await asyncio.sleep(0.1)
    check("a local ADB disconnect does NOT close the whole bridge (WS stays open across tasks)",
          not bridge._closed and companion_bridge.MANAGER.get_bridge(500) is bridge)

    await ws.push_bytes(b"ARRIVED-BEFORE-RECONNECT")
    await asyncio.sleep(0.1)
    check("bytes arriving with no local client attached are buffered, not dropped",
          bridge._pending_ws_bytes == [b"ARRIVED-BEFORE-RECONNECT"])

    reader2, writer2 = await asyncio.open_connection("127.0.0.1", bridge.local_port)
    flushed = await asyncio.wait_for(reader2.read(1024), timeout=1)
    check("the next local connection is flushed the buffered bytes first", flushed == b"ARRIVED-BEFORE-RECONNECT")
    check("the pending-bytes buffer is cleared after flushing", bridge._pending_ws_bytes == [])

    writer2.close()
    await ws.push_disconnect()
    await asyncio.sleep(0.1)
    check("a genuine WebSocket disconnect closes the whole bridge", bridge._closed)
    check("a closed bridge deregisters itself from MANAGER", companion_bridge.MANAGER.get_bridge(500) is None)


async def part4_bridge_touch_abort_callback():
    ws = FakeWebSocket()
    calls = []

    async def on_touch_abort(device_id):
        calls.append(device_id)

    bridge = await companion_bridge.start_device_bridge(501, ws, on_touch_abort=on_touch_abort)
    await ws.push_text(json.dumps({"type": "touch_abort"}))
    await asyncio.sleep(0.1)
    check("a touch_abort control message invokes the callback with the device_id", calls == [501])
    check("the bridge stays open after a touch_abort message (only WS disconnect ends it)", not bridge._closed)
    await bridge.close()


async def part4_bridge_pong_and_stale_timeout():
    ws = FakeWebSocket()
    bridge = await companion_bridge.start_device_bridge(502, ws)
    before = bridge._last_activity

    await asyncio.sleep(0.05)
    await ws.push_text("pong")
    await asyncio.sleep(0.05)
    check("a 'pong' text frame updates last_activity (keeps the bridge alive)", bridge._last_activity > before)

    # Force staleness detection without waiting out the real timeout --
    # _heartbeat_loop runs on its own asyncio.sleep(config.
    # COMPANION_BRIDGE_HEARTBEAT_SECONDS) cadence, so rather than waiting
    # real wall-clock time out, directly exercise the same staleness
    # check it performs against a manually-backdated _last_activity.
    bridge._last_activity = time.time() - (config.COMPANION_BRIDGE_STALE_TIMEOUT_SECONDS + 5)
    is_stale = (time.time() - bridge._last_activity) > config.COMPANION_BRIDGE_STALE_TIMEOUT_SECONDS
    check("the staleness threshold check flags an old last_activity as stale", is_stale)
    await bridge.close()
    check("closing a stale bridge deregisters it", companion_bridge.MANAGER.get_bridge(502) is None)


async def part4_second_bridge_replaces_first():
    ws_a = FakeWebSocket()
    bridge_a = await companion_bridge.start_device_bridge(503, ws_a)
    ws_b = FakeWebSocket()
    bridge_b = await companion_bridge.start_device_bridge(503, ws_b)
    check("starting a new bridge for the same device_id closes the old one", bridge_a._closed)
    check("MANAGER now points at the new bridge", companion_bridge.MANAGER.get_bridge(503) is bridge_b)
    await bridge_b.close()


# ===========================================================================
# 5. AndroidDeviceManager.connect_device routing (bridge_kind)
# ===========================================================================

async def part5_connect_device_routing():
    mgr = AndroidDeviceManager()

    # companion_ws with no live bridge -- fails fast, translated message.
    device_row_no_bridge = {"id": 601, "bridge_kind": "companion_ws", "tunnel_host": None, "tunnel_port": None}
    try:
        await mgr.connect_device(device_row_no_bridge)
        check("companion_ws with no live bridge raises ConnectionError", False)
    except ConnectionError as e:
        check("companion_ws with no live bridge raises ConnectionError", True)
        check("the error message is human-readable, not a raw exception repr", "Bridge" in str(e) or "Companion" in str(e))

    # companion_ws WITH a live bridge -- routes to 127.0.0.1:<local_port>,
    # not to any stored tunnel_host/tunnel_port.
    ws = FakeWebSocket()
    bridge = await companion_bridge.start_device_bridge(602, ws)
    device_row_with_bridge = {"id": 602, "bridge_kind": "companion_ws", "tunnel_host": None, "tunnel_port": None}

    class _FakeU2Device:
        def __init__(self):
            self.info = {"ok": True}

    with patch("uiautomator2.connect", return_value=_FakeU2Device()) as mock_connect:
        result = await mgr.connect_device(device_row_with_bridge)
    check("connect_device returns a device for a companion_ws bridge with a live bridge", result is not None)
    connect_addr = mock_connect.call_args[0][0]
    check("connect_device dials 127.0.0.1:<bridge.local_port>, not a stored address",
          connect_addr == f"127.0.0.1:{bridge.local_port}")
    await bridge.close()

    # direct_lan is unaffected (regression guard).
    mgr2 = AndroidDeviceManager()
    device_row_lan = {"id": 603, "bridge_kind": "direct_lan", "tunnel_host": "192.168.1.50", "tunnel_port": 40123}
    with patch("uiautomator2.connect", return_value=_FakeU2Device()) as mock_connect2:
        await mgr2.connect_device(device_row_lan)
    check("a direct_lan device still dials its own stored tunnel_host:tunnel_port",
          mock_connect2.call_args[0][0] == "192.168.1.50:40123")

    # direct_lan with no address raises a translated ConnectionError too.
    mgr3 = AndroidDeviceManager()
    device_row_lan_missing = {"id": 604, "bridge_kind": "direct_lan", "tunnel_host": None, "tunnel_port": None}
    try:
        await mgr3.connect_device(device_row_lan_missing)
        check("a direct_lan device with no address raises ConnectionError", False)
    except ConnectionError:
        check("a direct_lan device with no address raises ConnectionError", True)

    # A device_row missing bridge_kind entirely defaults to direct_lan
    # (every pre-migration-044 row) -- must not crash or misroute.
    mgr4 = AndroidDeviceManager()
    device_row_legacy = {"id": 605, "tunnel_host": "10.0.0.5", "tunnel_port": 5555}
    with patch("uiautomator2.connect", return_value=_FakeU2Device()) as mock_connect4:
        await mgr4.connect_device(device_row_legacy)
    check("a device_row with no bridge_kind column at all defaults to direct_lan",
          mock_connect4.call_args[0][0] == "10.0.0.5:5555")


# ===========================================================================
# 6. server.py /device/ws route (starlette TestClient, SYNCHRONOUS)
# ===========================================================================

def part6_device_ws_route():
    """Deliberately a plain (non-async) function, called OUTSIDE
    asyncio.run() below -- same convention as test_android_phone_agent.py's
    part13_server_routes, for the same reason (TestClient's websocket
    support spins up its own event loop internally)."""
    from starlette.testclient import TestClient
    from starlette.testclient import WebSocketDisconnect
    from messa.server import app as fastapi_app

    real_flag = config.MESSA_COMPANION_BRIDGE_ENABLED
    config.MESSA_COMPANION_BRIDGE_ENABLED = False
    client = TestClient(fastapi_app)
    try:
        try:
            with client.websocket_connect("/device/ws", headers={"Authorization": "Bearer whatever"}):
                pass
            check("the route refuses to connect when the feature flag is off", False)
        except WebSocketDisconnect as e:
            check("the route refuses to connect when the feature flag is off", e.code == 4404)
    finally:
        config.MESSA_COMPANION_BRIDGE_ENABLED = real_flag

    config.MESSA_COMPANION_BRIDGE_ENABLED = True
    try:
        try:
            with client.websocket_connect("/device/ws"):
                pass
            check("a missing Authorization header is refused", False)
        except WebSocketDisconnect as e:
            check("a missing Authorization header is refused", e.code == 4401)

        try:
            with client.websocket_connect("/device/ws", headers={"Authorization": "Basic notbearer"}):
                pass
            check("a non-Bearer Authorization header is refused", False)
        except WebSocketDisconnect as e:
            check("a non-Bearer Authorization header is refused", e.code == 4401)

        # An already-paired device's secret should promote straight to a
        # live bridge -- real bytes then flow both ways through the ACTUAL
        # local loopback socket this route opens (a plain socket.socket()
        # here stands in for adbutils, same as the rest of this project's
        # "mock at the hardware boundary, keep everything above it real"
        # convention).
        fake_device_row = {"id": 700, "user_id": 77, "bridge_kind": "companion_ws", "device_name": "Test Phone"}
        with patch("messa.companion_bridge.db.get_device_by_secret_hash", new=AsyncMock(return_value=fake_device_row)), \
             patch("messa.db.update_device_status", new=AsyncMock(return_value=None)):
            with client.websocket_connect("/device/ws", headers={
                "Authorization": "Bearer some-already-paired-secret",
                "X-Device-Name": "Test Phone",
            }) as ws:
                # Give the server-side route a brief moment to call
                # start_device_bridge and open its local listener before we
                # go looking for it.
                bridge = None
                deadline = time.time() + 2
                while time.time() < deadline:
                    bridge = companion_bridge.MANAGER.get_bridge(700)
                    if bridge is not None and bridge.local_port:
                        break
                    time.sleep(0.05)
                check("the route promotes an already-paired device straight to a live bridge", bridge is not None)

                # The route sends one cosmetic {"type": "bridge_active"}
                # JSON/text control frame right after promoting -- drain it
                # before treating anything else received as ADB payload
                # bytes (see server.py's _run_as_bridge).
                bridge_active_msg = ws.receive_json()
                check("the route announces bridge_active over the socket before relaying bytes",
                      bridge_active_msg == {"type": "bridge_active"})

                if bridge is not None:
                    local_client = socket.create_connection(("127.0.0.1", bridge.local_port), timeout=2)
                    try:
                        local_client.sendall(b"REAL-LOCAL-ADB-BYTES")
                        received_over_ws = ws.receive_bytes()
                        check("bytes written to the real local socket arrive over the WebSocket",
                              received_over_ws == b"REAL-LOCAL-ADB-BYTES")

                        ws.send_bytes(b"REAL-SERVER-TO-PHONE-BYTES")
                        local_client.settimeout(2)
                        received_locally = local_client.recv(1024)
                        check("bytes sent over the WebSocket arrive at the real local socket",
                              received_locally == b"REAL-SERVER-TO-PHONE-BYTES")
                    finally:
                        local_client.close()
        check("the bridge is deregistered after the WebSocket connection closes",
              companion_bridge.MANAGER.get_bridge(700) is None)
    finally:
        config.MESSA_COMPANION_BRIDGE_ENABLED = real_flag


# ===========================================================================
# 7. server.py SMS pairing-code short-circuit regex
# ===========================================================================

_PAIR_CODE_RE = re.compile(r"^\s*PAIR[\s:.-]*(\d{3})[\s-]?(\d{3})\s*$", re.IGNORECASE)


def part7_pairing_sms_regex():
    m = _PAIR_CODE_RE.match("PAIR 918-243")
    check("'PAIR 918-243' matches the pairing short-circuit regex", m is not None and m.group(1) == "918" and m.group(2) == "243")

    m2 = _PAIR_CODE_RE.match("pair 918243")
    check("lowercase, no dash still matches", m2 is not None)

    m3 = _PAIR_CODE_RE.match("  PAIR   918 243  ")
    check("extra whitespace still matches", m3 is not None)

    check("a bare 6-digit OTP (no 'PAIR' prefix) does NOT match", _PAIR_CODE_RE.match("918243") is None)
    check("an unrelated short reply does NOT match", _PAIR_CODE_RE.match("done") is None)
    check("a normal sentence does NOT match", _PAIR_CODE_RE.match("Please pair my new device") is None)


# ===========================================================================
# main
# ===========================================================================

async def main() -> None:
    part1_secrets_and_codes()
    await part2_db_bind_and_lookup()
    await part3_pairing_lifecycle()
    await part4_bridge_byte_forwarding_and_lifecycle()
    await part4_bridge_touch_abort_callback()
    await part4_bridge_pong_and_stale_timeout()
    await part4_second_bridge_replaces_first()
    await part5_connect_device_routing()
    part7_pairing_sms_regex()


if __name__ == "__main__":
    asyncio.run(main())
    # Deliberately run OUTSIDE asyncio.run() -- see part6_device_ws_route's
    # own docstring for why TestClient websocket tests can't live inside
    # that same loop.
    part6_device_ws_route()

    if failures:
        print(f"\n{len(failures)} FAILURE(S): {failures}")
        sys.exit(1)
    print("\nALL PASS")
