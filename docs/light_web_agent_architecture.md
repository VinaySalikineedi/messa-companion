# `light-web-agent` (v2.1 Production Specification)
## Universal Dynamic Web Navigation Agent Architecture

**Project**: Messa Autonomous AI Concierge  
**Target Component**: `light-web-agent` (Universal Dynamic Browser Engine)  
**Status**: Final Design Specification (Incorporating Claude Review & Production Edge-Case Hardening)  
**Replaces**: Legacy multi-agent crawler tree (`deepsearch_agent`)  

---

## 1. Executive Summary & Core Motivation

### Why Web Agents Fail in Production
1. **The Stateless Browser Teardown Antipattern (The Infinite OTP Loop)**:
   - Traditional web agents treat browser sessions as ephemeral function calls (`run() ➔ close()`).
   - When encountering a phone verification (SMS OTP) or 2FA challenge, the sub-agent exits, terminating the browser tab.
   - When the user texts back their verification code, the system spawns a *new* browser with blank cookies. The active sign-up form is lost, triggering a *new* OTP and making the user's code instantly obsolete.
2. **Brittle Rigid Selectors vs. Real-World Web Dynamism**:
   - Fixed-step workflows (`Click A ➔ Type B ➔ Click C`) crash the moment an unpredicted overlay appears (e.g. *"$0 delivery fee for new users"*, age gates, cookie consent modals, app install banners).
3. **The Single-Tab / Iframe Blindness Trap**:
   - SSO popups (*"Sign in with Google/Apple"*) or bank 3D-Secure windows open in secondary tabs or popups (`window.open`), causing single-tab agents to stare blindly at inactive parent pages.
   - Payment fields (credit card number, CVV) are hosted inside cross-origin iframes or Shadow Roots, rendering them invisible to naive DOM/A11y parsers.
4. **Token & Proxy Exhaustion**:
   - Feeding raw 100k-line HTML dumps into LLMs causes high latency and hallucinations.
   - Unfiltered ad networks, video players, and analytics trackers burn through expensive residential proxy bandwidth.

---

## 2. System Architecture Diagram

```
                              +-------------------------+
                              | User Request (via SMS)  |
                              +------------+------------+
                                           |
                                           v
                       +-------------------+--------------------+
                       | Messa Skill Registry & Macro Cache     |
                       | (Known Blueprints: Walmart, Instacart) |
                       +-------------------+--------------------+
                                           |
                       +-------------------+--------------------+
                       | Zero-LLM Landmark Prior Pass (Fast-Path)|
                       | Matches standard: Search, Cart, Sign-In|
                       +-------------------+--------------------+
                                           |
                                           v
+---------------------------------------------------------------------------------------+
|                              THE COGNITIVE ENGINE LOOP                                |
|                                                                                       |
|  1. PERCEIVE:                                                                         |
|     - Pruned Accessibility (A11y) Tree (<1,500 tokens) with interactive IDs: [e1]   |
|     - Recursive Frame & Shadow DOM indexing: [f1:e4]                                  |
|     - Active Page Stack Manager (Auto-switches to popups/OAuth tabs)                  |
|     - Visual Grounding Fallback (Computer-Use (x,y) if ARIA tree is empty)            |
|                                                                                       |
|  2. REFLECT & CRITIC:                                                                 |
|     - Compares state against goal & last action                                       |
|     - Detects & prioritizes dismissing blocking modals (e.g., "$0 delivery fee")       |
|     - Loop Detector & Circuit Breaker (Halts after 2 identical actions / 15 steps)    |
|                                                                                       |
|  3. PLAN & BATCH ACT:                                                                 |
|     - Macro-Action Batching with target_signature staleness checks                    |
|     - Tokenized Credentials (e.g., {{cred:walmart_password}} never in prompt)        |
|     - Structured Post-Conditions (expect: URL or DOM mutation)                        |
|                                                                                       |
|  4. VERIFY:                                                                           |
|     - Visual stabilization guard (debounces network/DOM quietness)                    |
|     - Evaluates assertion; auto-invalidates stale macro cache on mismatch             |
+------------------------------------------+--------------------------------------------+
                                           |
                                           v
                       +-------------------+--------------------+
                       | Decision: Action Execution vs. Gate    |
                       +---------+--------------------+---------+
                                 |                    |
                         Standard Execution     Human Checkpoint
                                 |                    |
                                 v                    v
              +------------------+----+     +---------+------------------+
              | Cloud Browser Engine  |     | Suspended Session State    |
              | - Browserbase Runtime |     | - Active Keepalive Pings   |
              | - US Residential Proxy|     | - 5-min Hard TTL Timer     |
              | - Ad/Tracker Shield   |     | - SMS Dispatched to User   |
              +-----------------------+     +---------+------------------+
                                                      |
                                               User Replies via SMS
                                                      |
                                                      v
                                            +---------+------------------+
                                            | Resume ACTIVE Tab Over CDP |
                                            | & Enter OTP / Verification |
                                            +----------------------------+
```

---

## 3. The 7 Core Architectural Pillars

### Pillar 1: Persistent Session & Suspended State (Zero-Kill OTP)
* **Session Lifetime**: Browser session handles (`session_id`, CDP WebSocket URL, cookies) belong to the **User's Active Task State**, stored in memory/Redis.
* **Keepalive & Zombie Billing Protection**:
  - While waiting for human input, a silent background heartbeat (`page.evaluate("1+1")`) runs every 40 seconds to prevent cloud inactivity timeouts.
  - **Hard 5-Minute TTL**: If the user fails to respond within 5 minutes, the session cleanly tears down, freeing resources and texting the user: *"Session paused for security. Reply 'continue' when ready."*
* **Stale OTP Recovery**: If the user replies after a code expires, the agent automatically clicks *"Resend Code"* and prompts the user for the fresh OTP.

### Pillar 2: Perception Engine with Frame & Shadow DOM Traversal
* **Pruned Accessibility (A11y) Tree**: Strips script tags, SVGs, invisible containers, and marketing trackers, assigning compact semantic IDs (`[e1]`, `[e2]`). Total prompt footprint: **< 1,500 tokens**.
* **Deep Iframe & Shadow DOM Recursion**: Payment gateways (Stripe, Cybersource) embed card inputs in iframes. The Perception Engine recursively traverses `page.frames` and open Shadow Roots, tagging them cleanly:
  ```json
  [
    {"id": "e1", "role": "button", "name": "Proceed to Checkout"},
    {"id": "f1:e1", "role": "textbox", "name": "Card Number", "in_frame": "stripe-card-element"},
    {"id": "f1:e2", "role": "textbox", "name": "CVC", "in_frame": "stripe-card-element"}
  ]
  ```
* **Active Tab & Popup Stack Manager**: Listens to Playwright `context.on("page")`. When *"Sign in with Google"* or a 3D-Secure bank verification spawns a popup (`window.open`), focus shifts automatically to the popup, completing the login and returning to the parent tab upon closure.

### Pillar 3: Zero-LLM "Web Convention Prior" (Fast-Path Navigation)
* Standard web interfaces follow predictable conventions: Search bars are top-center; Login and Carts are top-right.
* **Header/Nav Landmark Pass**: A sub-millisecond deterministic synonym matcher scans the A11y tree for standard anchors (`Sign In`, `Log In`, `Cart`, `Search`, `Checkout`).
* **Impact**: Common navigation steps execute **instantly with 0 LLM token cost**.

### Pillar 4: Macro-Action Batching with Staleness Checks
* **The Problem**: 1 LLM call per single keystroke takes 15–20 seconds for a basic form.
* **The Solution**: The agent emits a batched `MacroAction` (e.g. `[type name, type email, click continue]`).
* **Target Signature Guard**: Every step records a hash of the target element (`role + name + visual_bounds`). If an unexpected modal interrupts the flow mid-batch, the signature fails to match, and the batch **aborts at that exact step**, preventing blind clicks into incorrect elements.

### Pillar 5: Security, Credential Tokenization & Prompt Injection Defense
* **Credential Isolation**: Secrets (passwords, card CVVs) are NEVER placed in LLM prompt strings or logs. They are referenced as tokens:
  ```python
  MacroStep(action="type", target="f1:e1", value="{{cred:user_payment_card}}")
  ```
  The browser-level runtime resolves tokens securely from Messa's encrypted vault.
* **Indirect Prompt Injection Shield**: Text extracted from untrusted web pages is wrapped in strict delimiters (`<web_content_untrusted>`) with instructions forbidding the execution of embedded commands.

### Pillar 6: Generalized Human Checkpoints
Human-in-the-Loop is generalized into four distinct checkpoint types:
1. `otp`: SMS 6-digit verification code.
2. `captcha`: If bot defense triggers an interactive puzzle, Messa sends an interactive **Live View URL** to the user: *"Tap here to solve verification on your screen in 5 seconds."*
3. `payment_3ds`: Bank mobile app approval notification.
4. `risk_review`: High-value cart confirmation (*"Walmart order total is $112.40. Reply YES to confirm purchase."*).

### Pillar 7: Network & Bandwidth Shield + Circuit Breakers
* **Selective Request Interceptor**: Aborts video streams, tracking scripts (*Criteo, DoubleClick, TikTok, Omniture*), and marketing fonts before download over the residential proxy. Preserves high-res product photos and logos intact for visual fidelity.
* **Bandwidth Savings**: Reduces per-session proxy data from **~12 MB down to ~1.5 MB** (85% reduction).
* **Circuit Breaker Limits**:
  - **Max Steps**: 15 atomic steps per task.
  - **Loop Interrupt**: Halts if the same action occurs twice consecutively with zero DOM change.
  - **Cost Cap**: $0.35 total LLM budget per task.

---

## 4. Structured Action Primitives

```python
from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field

class MacroStep(BaseModel):
    action: Literal["click", "type", "press_key", "scroll", "select_option", "wait_for"]
    target_id: str                      # e.g., "e3" or "f1:e2"
    target_signature: str               # sha256(role:name:tag) for staleness validation
    value: Optional[str] = None         # text or token (e.g., "{{cred:password}}")

class MacroAction(BaseModel):
    steps: List[MacroStep]
    expect: Dict[str, Any]              # Assertion: e.g., {"url_contains": "/checkout"}

class HumanCheckpoint(BaseModel):
    kind: Literal["otp", "captcha", "payment_3ds", "risk_review"]
    prompt_to_user: str                 # SMS message sent to user
    interactive_url: Optional[str] = None # Live View link if captcha/biometrics needed
    timeout_seconds: int = 300          # Default 5-minute keepalive window

class AgentDecision(BaseModel):
    thought: str                        # Chain of thought reflection & anomaly check
    action: Optional[MacroAction] = None
    checkpoint: Optional[HumanCheckpoint] = None
    is_complete: bool = False
    result_summary: Optional[str] = None
```

---

## 5. Implementation Roadmap (Branch: `feature/light-web-agent`)

### Phase 1: Core Engine & Persistent Session State Manager
- [ ] Create `messa/channels/light_web_agent.py` and `messa/channels/browser_session_manager.py`.
- [ ] Implement `SessionManager` storing active CDP connections and page instances.
- [ ] Implement 40s heartbeat keepalive pings and 5-minute hard TTL auto-teardown.
- [ ] Build `PageStackManager` listening to `context.on("page")` for auto-popup/tab switching.

### Phase 2: Perception Engine & Network Shield
- [ ] Build recursive `extract_pruned_a11y_tree()` traversing top-level DOM, iframes, and Shadow Roots.
- [ ] Add `target_signature` generator for element validation.
- [ ] Wire request-level media/tracker interceptor for residential proxy bandwidth reduction.
- [ ] Add zero-LLM landmark dictionary scanner (`Sign In`, `Search`, `Cart`).

### Phase 3: Cognitive Decision Loop & Reflection Engine
- [ ] Implement the `Sense-Reflect-Act-Verify` loop powered by Claude 3.5 Sonnet (with GPT-4o fallback).
- [ ] Build obstacle reflection engine (auto-detecting and dismissing promo modals/popups).
- [ ] Integrate Circuit Breakers (15-step cap, 2x loop detector, cost tracking).
- [ ] Add Computer-Use coordinate visual fallback when A11y trees lack matches.

### Phase 4: Macro-Action Batching & Skill Cache
- [ ] Build `MacroExecutor` executing batched actions with mid-flight staleness abortion.
- [ ] Implement `CredentialVault` token replacement (`{{cred:...}}` resolved locally).
- [ ] Build Self-Healing Skill Cache (stores winning macro sequences; invalidates on `expect` failure).

### Phase 5: Human Checkpoint Relay & SMS Webhook Integration
- [ ] Implement `pause_for_human()` dispatching SMS via Messa's messaging channel.
- [ ] Build incoming SMS resume hook to reconnect active sessions and input OTP codes.
- [ ] Implement Live View URL generation for manual user CAPTCHA/3DS takeover.

### Phase 6: End-to-End Benchmarks & Production Cutover
- [ ] Benchmark 1: Walmart end-to-end account creation with live SMS OTP verification.
- [ ] Benchmark 2: Instacart guest shopping & cart population with promotional popup interruption.
- [ ] Benchmark 3: Multi-tab Google SSO login flow.
- [ ] Deprecate legacy `deepsearch_agent` and route all autonomous browser tasks through `light-web-agent`.
