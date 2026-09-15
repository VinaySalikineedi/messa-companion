# Messa Companion APK

The Android side of the open-source, no-VPN reverse tunnel described in
`open-source-phone.md` sections 2-4 (feature/messa-companion-apk). This
app is what lets Messa's server reach a phone it can never dial into
directly (Hugging Face Spaces is HTTP/WebSocket-only; most home routers'
NAT/CGNAT block unsolicited inbound connections regardless) -- instead of
the server connecting to the phone, this app's persistent foreground
service dials **out** to the server over a normal `wss://` WebSocket, and
the server exposes that connection back to `adbutils`/`uiautomator2` as
an ordinary local ADB socket (see `messa/companion_bridge.py`'s own
module docstring for the server side of this).

## Why this exists

* Hugging Face Spaces / most cloud hosts only expose HTTP(S) and
  WebSocket ports -- no raw inbound TCP/UDP a phone could dial into, and
  no budget for a third-party VPN's per-seat licensing.
* A phone always dials OUT successfully through any router, NAT, or
  CGNAT, because it's the one initiating the connection.
* Standard `adbutils`/`uiautomator2` code on the server never changes --
  from their point of view they're just connecting to
  `127.0.0.1:<some port>`, exactly like a phone on the same LAN would
  present. All the tunneling is invisible to them.

## Project layout

```
companion-apk/
  settings.gradle.kts, build.gradle.kts, gradle.properties   -- root Gradle config (Kotlin DSL)
  app/build.gradle.kts                                       -- app module: AndroidX, OkHttp, coroutines
  app/src/main/AndroidManifest.xml
  app/src/main/kotlin/ai/messa/companion/
    DeviceSecretManager.kt       -- section 3.2: Keystore-backed 256-bit device secret
    NsdAdbPortDiscovery.kt       -- roadblock #1: mDNS discovery of the local ADB port
    AdbLocalSocketRelay.kt       -- phone-side half of the byte-pipe (local ADB <-> callbacks)
    WebSocketBridgeClient.kt     -- outbound `/device/ws` OkHttp WebSocket client
    BridgeForegroundService.kt   -- roadblock #3/#5: the persistent foreground service that wires it all together
    TouchKillswitchOverlay.kt    -- section 3.4: physical-touch abort overlay
    BridgeConfig.kt              -- server URL (overridable for self-hosting)
    BridgeStatus.kt              -- tiny status bus, service -> UI
    MainActivity.kt              -- the app's one screen
  app/src/main/res/...
```

## Security model (section 3), as implemented here

* **Zero inbound ports.** This app never opens a listening socket of any
  kind -- `AdbLocalSocketRelay` only ever makes an OUTBOUND local
  connection to the phone's own ADB daemon, and `WebSocketBridgeClient`
  only ever makes an OUTBOUND connection to the server. Nothing on this
  phone is ever reachable from the network.
* **Device secret.** `DeviceSecretManager` generates a 256-bit random
  secret on first use and stores it exclusively in Android
  Keystore-backed `EncryptedSharedPreferences`. It is presented on every
  `/device/ws` connection as `Authorization: Bearer <secret>` and is
  never logged (grep this codebase -- no call site logs the secret
  itself). The server only ever stores a sha256 hash of it (migration
  044's `device_secret_hash`).
* **SMS pairing handshake.** A not-yet-paired connection gets a 6-digit
  code back from the server (`WebSocketBridgeClient.Listener.onPairingRequired`,
  shown in `MainActivity`); the user texts it from their registered
  Messa number, and only that SMS actually creates the
  `user_devices` row (`messa/companion_bridge.py`'s `bind_pairing_code`).
  Whoever holds the phone can never bind it to an account by connecting
  alone.
* **Touch killswitch.** `TouchKillswitchOverlay` is armed for the
  duration of any task (shown/hidden by `BridgeForegroundService` around
  each local ADB connection) and fires `{"type":"touch_abort"}` the
  instant a genuine finger touch is detected, which the server maps
  straight to the same Home-key-press + task-cancel path the live-view
  page's own "Pause / Abort" button uses. **Flagged for hardware
  verification** -- see that file's own doc comment for the heuristic
  used (`MotionEvent.getDeviceId()`) and its known limits; this sandbox
  has no physical Android device or emulator to verify it against.

## Roadblocks from section 4, and where they're handled

| Roadblock | Handled in |
|---|---|
| Dynamic wireless-debugging port | `NsdAdbPortDiscovery.kt` |
| One-time ADB RSA "Always allow" prompt | User-facing instruction only -- nothing to automate on the APK side |
| Battery optimization / Doze | `MainActivity.requestBatteryOptimizationExemptionIfNeeded` + `BridgeForegroundService`'s `START_STICKY` foreground service |
| Cloudflare/HF idle-WebSocket timeout | Heartbeat: server pings every 25s (`config.COMPANION_BRIDGE_HEARTBEAT_SECONDS`), `WebSocketBridgeClient` replies `pong` |
| Android 14+ foreground service restrictions | `AndroidManifest.xml`'s `android:foregroundServiceType="connectedDevice"` + matching permission |

## Build status / what has NOT been verified here

This project was written and committed from a cloud sandbox with **no
Android SDK, no Gradle Android plugin, no emulator, and no network access
to Maven Central** (only a small allowlist of package registries is
reachable) -- so none of the following could be done in this
environment, and are called out explicitly rather than silently assumed:

* **No `gradle build` / `gradle assembleDebug` was run.** The Gradle
  Kotlin DSL files are hand-written to standard, current (AGP 8.5.2 /
  Kotlin 1.9.24) conventions, and every Kotlin source file was checked
  with a real `kotlinc` for balanced braces and for anything beyond the
  *expected* "unresolved reference" errors from missing
  Android/OkHttp/AndroidX jars (none found) -- but a full, dependency-
  resolved compile has not happened. **Open this in Android Studio and
  run a Gradle sync + build before treating this as done.**
* **`gradle-wrapper.jar` is not committed** (see
  `gradle/wrapper/gradle-wrapper.properties`'s own comment) -- Android
  Studio regenerates it on first open, or run `gradle wrapper` once from
  a machine with a real Gradle install.
* **No physical device or emulator testing** -- the touch-killswitch
  device-id heuristic, the mDNS discovery against a real wireless-
  debugging daemon, and the actual byte-for-byte ADB handshake over the
  bridge all need a real phone to confirm. This mirrors the same
  "ship the server-side engine on mocks, flag hardware verification"
  decision already made for `feature/open-source-phone`'s
  `AndroidPhoneAgent`.
* **Launcher icon is a placeholder** (`ic_launcher_foreground.xml`) --
  functional (valid adaptive icon XML, minSdk 26+ only needs this
  format), but cosmetic; swap for real branding before a store release.

## Server-side counterpart

* `messa/companion_bridge.py` -- the pairing manager and
  `CompanionDeviceBridge` (the server's own half of the byte-pipe).
* `messa/server.py`'s `@app.websocket("/device/ws")` route, and the SMS
  pairing-code short-circuit in `_process_inbound`.
* `messa/devices/android.py`'s `AndroidDeviceManager.connect_device` --
  routes a `bridge_kind == 'companion_ws'` device through the live
  bridge's local port instead of a stored `tunnel_host`/`tunnel_port`.
* `migrations/044_companion_apk_bridge.sql` -- schema.
* `tests/test_android_companion_bridge.py` -- automated tests for all of
  the above (mocked; no real device/phone involved, same convention as
  the rest of this project's Android-related test suite).
