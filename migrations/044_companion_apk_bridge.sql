-- Migration 044: Messa Companion APK reverse-tunnel bridge
-- (open-source-phone.md section 2/3, feature/messa-companion-apk)
--
-- Adds support for a SECOND way a user_devices row can be reached, on top
-- of the existing direct-LAN path (migration 043): instead of Messa's
-- server dialing INTO the phone's own tunnel_host:tunnel_port (which only
-- ever works when the server and the phone share a reachable network --
-- exactly the limitation that motivated this bridge), the Messa Companion
-- APK dials OUT to messa/server.py's `/device/ws` over a normal outbound
-- wss:// connection, and the server exposes that connection to
-- adbutils/uiautomator2 as a local loopback TCP socket (messa/
-- companion_bridge.py). Nothing about AndroidDeviceManager/
-- AndroidPhoneAgent changes for this -- to that code, a companion-bridged
-- device just has a different (host, port) to connect to: 127.0.0.1 and
-- an ephemeral, in-memory-only port assigned when the bridge socket comes
-- up (see companion_bridge.py's own header for why that port is
-- deliberately NOT persisted here, same "can't survive a restart anyway"
-- reasoning as migration 043's tunnel_host/tunnel_port already accepts
-- for the direct-LAN case).
--
-- bridge_kind distinguishes the two paths on the SAME user_devices table
-- (not a new table) since everything else about a device row -- name,
-- status, last_seen_at, family-sharing authorizations, published skills --
-- is identical regardless of which transport reaches it.

ALTER TABLE user_devices
    ADD COLUMN IF NOT EXISTS bridge_kind VARCHAR(20) NOT NULL DEFAULT 'direct_lan';
    -- 'direct_lan'   -- messa/devices/android.py connects straight to
    --                   tunnel_host:tunnel_port (migration 043's original
    --                   design -- still valid when the server and the
    --                   phone genuinely share a reachable network).
    -- 'companion_ws' -- the phone's Messa Companion APK holds an outbound
    --                   WebSocket open to /device/ws; connect through
    --                   companion_bridge.py's live CompanionDeviceBridge
    --                   for this device_id instead of tunnel_host/port.

-- A companion_ws device has no meaningful tunnel_host/tunnel_port at
-- pairing time (there is no LAN address to record -- the whole point is
-- that one was never reachable). Direct-LAN rows keep populating both as
-- before; migration 043 required both NOT NULL, which this loosens.
ALTER TABLE user_devices ALTER COLUMN tunnel_host DROP NOT NULL;
ALTER TABLE user_devices ALTER COLUMN tunnel_port DROP NOT NULL;

-- device_secret_hash: sha256 hex digest of the companion APK's own
-- cryptographically-random 256-bit device secret (open-source-phone.md
-- section 3.2) -- the bearer credential the APK presents on every
-- `/device/ws` connection (`Authorization: Bearer <secret>`). Only the
-- HASH is ever stored server-side; the raw secret lives only in the
-- phone's own EncryptedSharedPreferences and in that one Authorization
-- header on the wire (over TLS) -- never logged, never persisted in the
-- clear, so a database read alone can never be replayed as a working
-- credential. NULL for a direct_lan device (it has no secret at all).
ALTER TABLE user_devices
    ADD COLUMN IF NOT EXISTS device_secret_hash VARCHAR(64);

-- A secret hash must be unique across every paired device -- a collision
-- (practically impossible for a real 256-bit random value, but this is
-- the actual security boundary a WS auth lookup relies on, so it's
-- enforced in the schema, not just assumed) would let one phone's secret
-- authenticate as a different user's device. Partial (WHERE ... NOT NULL)
-- since direct_lan rows leave this NULL and NULL is never unique-compared
-- against another NULL anyway, but being explicit here matches this
-- table's existing style of documenting exactly what's enforced and why.
CREATE UNIQUE INDEX IF NOT EXISTS ix_user_devices_secret_hash
    ON user_devices (device_secret_hash)
    WHERE device_secret_hash IS NOT NULL;

-- Deliberately NOT adding a table for in-flight pairing (the 6-digit code
-- shown on the APK before a user_devices row exists to bind it to). That
-- state is scoped to exactly one live, in-process WebSocket connection
-- that hasn't been claimed by an SMS yet -- if the server restarts, the
-- APK's own connection drops too and the code is meaningless regardless,
-- the same "nothing durable to persist" reasoning live_activity.py,
-- call_activity.py, phone_activity.py, and deepsearch_control.py all
-- already document for their own in-memory-only state. See
-- messa/companion_bridge.py's CompanionBridgeManager.
