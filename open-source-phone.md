# Messa Open-Source Phone: Bring Your Own Phone (BYOP)

## 1. Executive Summary & Vision

The **Messa Open-Source Phone** feature allows any user to pair their physical Android phone with Messa over SMS/iMessage. Once paired, Messa acts as an autonomous on-device agent capable of opening apps, ordering food, calling rides, managing settings, navigating dashboards, and executing repetitive tasks on the user's actual device.

### Core Tenets
1. **Zero Third-Party Subscriptions ($0 Cost Forever)**: No dependence on commercial VPNs like Tailscale. All network bridging runs natively over standard outbound WebSockets (`wss://`) hosted directly on Messa (e.g. Hugging Face Spaces / VPS).
2. **Zero PC Required on User Side**: Onboarding must be executable purely from the phone in under 60 seconds.
3. **Conversational Self-Healing**: Messa never throws raw stack traces or silent failures over text; it diagnoses connectivity problems (expired pairing codes, closed ports, disabled debugging) and guides the user via text.
4. **Multi-User Family Sharing**: The phone owner can authorize family members by email/phone. Requests are queued sequentially since a physical phone can only run one app in the foreground.
5. **Visual Accountability**: Milestone achievement screenshots (e.g. food delivery cart totals, ride confirmations) are pushed directly into the text thread via MMS before placing orders, plus an optional live stream on Messa's `/live/<token>/phone` dashboard.
6. **Universal & Community-Generated Skills**: Common app workflows are pre-packaged. When Messa encounters an unknown app, it explores the UI hierarchy, synthesizes a reusable automation recipe, and optionally publishes it to a cloud registry with author attribution.
7. **Strict Tier & Resource Guardrails**: Pre-flight task estimators and hard step limits protect server compute and LLM token budgets against massive multi-day task injections and prompt exploits.

---

## 2. Production Network Bridge: The Messa Companion APK

To avoid third-party VPN licensing traps and work seamlessly on Hugging Face Spaces (which only exposes HTTP/WebSocket ports and blocks raw inbound UDP), Messa uses a dedicated open-source Android Companion APK (**Messa Bridge**).

```
┌─────────────────────────────────────────────────────────────┐
│                       USER'S PHONE                          │
│  • Local ADB Daemon: 127.0.0.1:<port>                       │
│  • Messa Companion APK (Persistent Foreground Service)     │
│    Pipes local ADB socket to outbound TLS WebSocket         │
└──────────────────────────────┬──────────────────────────────┘
                               │ Outbound TLS 1.3 (wss://.../device/ws)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│                 MESSA SERVER (HUGGING FACE)                 │
│  • FastAPI WebSocket Handler: /device/ws                    │
│  • Virtual Local ADB Loopback Socket                        │
│  • adbutils & uiautomator2 (Unmodified Python Engine)       │
│  • Device Queue: Exclusive Mutex Lock per Phone             │
│  • Live View Server: Streams frames to messa.ai/live/<token>│
└─────────────────────────────────────────────────────────────┘
```

### Why This Architecture Wins
* **100% Free & Legally Unencumbered:** Standard WebSocket traffic over HTTPS (port 443). Zero per-seat fees, zero commercial license restrictions.
* **Bypasses All Routers & Firewalls:** Because the phone initiates an *outbound* connection, it punches through home Wi-Fi NAT, hotel portals, and cellular CGNAT (5G/LTE) without port forwarding.
* **Preserves Standard ADB & `uiautomator2`:** The Python server pipes the WebSocket data into a local virtual socket. To `adbutils` and `uiautomator2`, the device appears as a standard local ADB connection.

---

## 3. Security & Anti-Leak Architecture

To guarantee that **no unauthorized person can access the phone** and that **no sensitive user data leaks**, the companion APK and Messa server enforce strict security boundaries:

```
┌────────────────────────────────────────────────────────────────────────┐
│                        SECURITY SAFEGUARDS                             │
├────────────────────────────────────────────────────────────────────────┤
│ 1. Zero Inbound Ports: Phone only makes OUTBOUND TLS (wss://) calls.   │
│ 2. Cryptographic Device Secret: 256-bit token generated on-device.     │
│ 3. One-Time 6-Digit SMS Handshake: 3-minute expiry, max 3 attempts.   │
│ 4. Single-Tenant Binding: Device ID strictly locked to user_id in DB.  │
│ 5. Hardware FLAG_SECURE: Banking apps & passwords black out automatically.│
│ 6. Physical Touch Killswitch: Any user touch aborts active automation. │
└────────────────────────────────────────────────────────────────────────┘
```

1. **Zero Inbound Ports on Phone:** The APK opens no listening ports on the local network or internet. It cannot be scanned or targeted by network attackers.
2. **Cryptographic Device Secret:** On first launch, the APK generates a cryptographically secure 256-bit random token stored in Android Keystore (`EncryptedSharedPreferences`). Every WebSocket connection must supply this secret in the `Authorization: Bearer <token>` header.
3. **SMS Handshake & Single-Tenant Binding:**
   * APK displays a 6-digit code (e.g. `PAIR 918-243`) valid for 3 minutes.
   * User texts this code from their registered phone number.
   * Messa validates the sender against `users.id` in Postgres and binds the device exclusively to that user.
   * No other phone number or user can command or access that device.
4. **Hardware `FLAG_SECURE` Protection:** Android OS automatically blacks out banking apps, password fields, and credit card numbers from ADB/media projection screen streams.
5. **Physical Touch & Notification Killswitch:**
   * A persistent Android notification with an **"Emergency Stop / Disconnect"** button.
   * If the user physically touches their phone screen while Messa is working, the APK immediately pauses the stream, presses Home, and signals Messa to release the lock.

---

## 4. Known Roadblocks & Concrete Architectural Solutions

| Roadblock | Why It Happens | Architectural Solution |
| :--- | :--- | :--- |
| **1. Dynamic Wireless Debugging Port** | Android changes the local port (e.g. `:41235`) on Wi-Fi reconnect. | **mDNS Loopback Discovery:** Modern Android advertises Wireless Debugging over mDNS (`_adb-tls-connect._tcp`). The APK uses Android's native `NsdManager` to auto-detect the local port on `127.0.0.1` without user input. |
| **2. One-Time ADB RSA Prompt** | First ADB connect triggers *"Allow USB debugging? Always allow from this computer"*. | **User Prompt Guidance:** The APK displays a 1-time instruction: *"Tap 'Always allow' on the prompt that appears."* Once checked, Android persists the RSA key permanently. |
| **3. Android Battery Optimization (Doze Mode)** | Android kills background apps when the phone is idle or screen is off. | **Foreground Service:** The APK runs as a sticky Android Foreground Service (`START_STICKY`) with a persistent notification and requests `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`. |
| **4. Cloudflare / Hugging Face WebSocket Timeout** | Proxies drop idle WebSockets after 30–60 seconds of inactivity. | **25-Second Heartbeat Ping/Pong:** Both the APK and `messa/server.py` exchange periodic ping/pong frames every 25 seconds to keep the TCP pipe active indefinitely. |
| **5. Android 14+ Foreground Service Restrictions** | Android 14 (API 34) requires explicit service types. | **Service Declaration:** The APK declares `android:foregroundServiceType="connectedDevice|specialUse"` in `AndroidManifest.xml` with proper intent filters. |

---

## 5. Conversational Guided Onboarding & Self-Healing

When a user interacts with Messa over SMS, Messa diagnoses connection issues conversationally:

| Error Condition | Internal Error Code | Messa's Conversational Text Response |
| :--- | :--- | :--- |
| **Wireless Debugging Disabled** | `ECONNREFUSED` / Timeout | *"It looks like Wireless Debugging turned off (Android disables this automatically when reconnecting to Wi-Fi). Go to **Settings > Developer Options > Wireless Debugging** and toggle it back on."* |
| **APK Disconnected** | `WS_CLOSED` / No active socket | *"Your Messa Companion app isn't connected. Please open the Messa app on your phone to reconnect."* |
| **Pairing Code Expired** | `PAIRING_CODE_EXPIRED` | *"That pairing code expired. Open the Messa Companion app on your phone and text me the fresh 6-digit code shown on screen."* |
| **Screen Locked / Sleeping** | Keyguard active | *"Your phone screen is locked. Please unlock it with your PIN or fingerprint so I can launch the app."* |

---

## 6. Family Sharing & Sequential Device Queue

* **Sequential Execution:** Since a physical phone can only show one foreground app at a time, concurrent family tasks are managed via a per-device `asyncio.Lock` queue.
* **Granular Category Enforcement:**
  * **Allowed Categories:** Food delivery, Rides, Smart home, Streaming, Maps.
  * **Strictly Blocked:** Banking, SMS/Messages, WhatsApp, Photos, Phone Settings (enforced in `messa/tools/android_phone_tools.py`).

---

## 7. Live View & Milestone Achievement Screenshots

* **Live Monitoring:** Real-time tile streaming to `messa.ai/live/<token>/phone` with a working **Pause/Abort** break-glass button.
* **Milestone MMS Proofs:** Before executing consequential actions (e.g. food delivery checkout), Messa captures the cart summary and texts the screenshot via MMS (`sendblue.send_message(media_url=...)`) with a prompt:
  > *"Here is your cart total ($18.50). Reply YES to authorize placing the order or NO to cancel."*

---

## 8. Instructions for Claude (Sandbox Implementation Guidelines)

When building the **Messa Companion APK** and the **Server WebSocket Bridge**:

### Critical Workflow Rules
1. **Local Branching (NO `.tar` or `.zip` files):**
   * Work exclusively on a local git feature branch: `feature/messa-companion-apk`.
   * Commit all code changes directly to the branch with clean, descriptive commit messages.
   * Do NOT package work into `.tar` or `.zip` archives.
2. **Component Architecture:**
   * **`companion-apk/`**: Standalone, clean Android Studio project using Kotlin, Gradle Kotlin DSL (`build.gradle.kts`), AndroidX, and OkHttp WebSocket client.
   * **`messa/server.py`**: Add `@app.websocket("/device/ws")` endpoint handling authentication, heartbeats, and binary socket bridging.
   * **`messa/devices/android.py`**: Add support for routing ADB traffic through the WebSocket virtual socket while reusing the existing `AndroidPhoneAgent` and `uiautomator2` logic.
3. **Rigorous Automated Testing:**
   * Provide comprehensive mock-based unit tests in `tests/test_android_companion_bridge.py`.
   * Test WebSocket authentication, heartbeats, binary frame forwarding, disconnection handling, and error translation.
   * Ensure all tests pass with `python tests/test_android_companion_bridge.py`.
