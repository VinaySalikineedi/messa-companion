# `light-web-agent` (v2.2 Production Specification)
## Universal Dynamic Web Navigation Agent Architecture

**Project**: Messa Autonomous AI Concierge  
**Target Component**: `light-web-agent` (Universal Dynamic Browser Engine)  
**Status**: Final Design Specification (v2.1 hardening + injection-defense, convention-prior, and drift-monitoring restoration)  
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
                                           v
                       +-------------------+--------------------+
                       | Zero-LLM Web Convention Prior (Fast-Path)|
                       | - Landmark/synonym match (Search, Cart, |
                       |   Sign-In, Checkout steps)               |
                       | - Direct-URL shortcuts (/cart, /account)|
                       | - Dark-pattern flag (pre-checked add-ons,|
                       |   guilt-trip decline copy)               |
                       +-------------------+--------------------+
                              high-conf. |          | low-conf. / novel layout
                                         |          |
+----------------------------------------v----------v-----------------------------------+
|                              THE COGNITIVE ENGINE LOOP                                |
|                                                                                       |
|  1. PERCEIVE:                                                                         |
|     - Pruned Accessibility (A11y) Tree (<1,500 tokens) with interactive IDs: [e1]   |
|     - Recursive Frame & Shadow DOM indexing: [f1:e4]                                  |
|     - Active Page Stack Manager, ORIGIN-ALLOWLISTED (auto-switches to popups/OAuth    |
|       tabs only if popup origin matches a known SSO/3DS allowlist; otherwise routes    |
|       to Human Checkpoint instead of auto-interacting)                                |
|     - Visual Grounding Fallback (Computer-Use (x,y) if ARIA tree is empty) — subject   |
|       to the SAME injection policy as text content (see Pillar 5)                     |
|                                                                                       |
|  2. REFLECT & CRITIC:                                                                 |
|     - Compares state against goal & last action                                       |
|     - Detects & prioritizes dismissing blocking modals (e.g., "$0 delivery fee")       |
|     - Loop Detector & Circuit Breaker (halts on 2x identical action, OR short          |
|       oscillating cycles like A→B→A→B across the last 6 actions, OR 15-step cap)      |
|                                                                                       |
|  3. PLAN & BATCH ACT:                                                                 |
|     - Macro-Action Batching; target_signature captured only AFTER the Visual           |
|       Stabilization Guard confirms DOM/network quiescence (not mid-reflow)            |
|     - Tokenized Credentials (e.g., {{cred:walmart_password}} never in prompt)        |
|     - Structured Post-Conditions (expect: URL or DOM mutation)                        |
|     - Consequential actions (order submit, address change, payment add, account       |
|       creation) are flagged is_consequential=True and routed to the Intent-           |
|       Alignment Critic below BEFORE execution                                         |
|                                                                                       |
|  4. VERIFY:                                                                           |
|     - Visual stabilization guard (debounces network/DOM quietness)                    |
|     - Evaluates assertion; auto-invalidates stale macro cache on mismatch             |
+------------------------------------------+--------------------------------------------+
                                           |
                                           v
                       +-------------------+--------------------+
                       | Decision: Action Execution vs. Gate    |
                       +----------+---------------------+-------+
                                  |                     |
                     non-consequential          consequential action
                                  |              (order submit, address
                                  |               change, payment add,
                                  |               account creation)
                                  |                     |
                                  |                     v
                                  |         +-----------+-----------+
                                  |         | Intent-Alignment      |
                                  |         | Critic — sees ONLY the|
                                  |         | user's original goal  |
                                  |         | + proposed action, NOT|
                                  |         | the page content that |
                                  |         | produced it           |
                                  |         +-----+-------------+---+
                                  |          aligned |    misaligned/uncertain
                                  v                v                 v
              +-------------------+---+  +---------+---+   +--------+-----------------+
              | Cloud Browser Engine  |  | (join back  |   | Human Checkpoint         |
              | - Browserbase Runtime |  |  to exec)   |   | (risk_review / block)    |
              | - US Residential Proxy|<-+-------------+   +--------------------------+
              | - Ad/Tracker Shield   |
              +-----------+-----------+          Standard Execution
                          |                              |
                          v                              v
              +-----------+------------+     +-----------+----------------+
              | Suspended Session State |     | Result flows back to       |
              | - Active Keepalive Pings|     | Skill Cache (win) or        |
              | - 5-min Hard TTL Timer  |     | Continuous Eval (drift)     |
              | - SMS Dispatched to User|     +-----------------------------+
              +-----------+--------------+
                          |
                  User Replies via SMS
                          |
                          v
              +-----------+----------------+
              | Resume ACTIVE Tab Over CDP  |
              | & Enter OTP / Verification  |
              +------------------------------+

   [background, always-on]
   +--------------------------------------------------------+
   | Continuous Evaluation & Drift Monitor                  |
   | - Nightly regression re-run of every skill blueprint   |
   | - Challenge/block-rate tracking per site + proxy pool  |
   | - Auto-invalidates Skill Cache entries on drift/failure|
   +--------------------------------------------------------+
```

---

## 3. The 8 Core Architectural Pillars

### Pillar 1: Persistent Session & Suspended State (Zero-Kill OTP)
* **Session Lifetime**: Browser session handles (`session_id`, CDP WebSocket URL, cookies) belong to the User's Active Task State, stored in memory/Redis.
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
* **Active Tab & Popup Stack Manager, Origin-Allowlisted**: Listens to Playwright `context.on("page")`. When *"Sign in with Google"* or a 3D-Secure bank verification spawns a popup (`window.open`), the manager checks the popup's origin against a maintained allowlist (`accounts.google.com`, `appleid.apple.com`, the parent site's own registrable domain, known 3DS issuers such as `*.cardinalcommerce.com`) before auto-focusing and interacting with it:
  - **Allowlisted origin** → focus shifts automatically, completes the login/verification, and returns to the parent tab on close.
  - **Non-allowlisted origin** → the popup is treated as untrusted and routed to a `risk_review` Human Checkpoint instead of auto-interacted with. Auto-following any spawned popup is a phishing risk; origin-awareness makes the convenience safe.

### Pillar 3: Zero-LLM "Web Convention Prior" (Fast-Path Navigation)
Standard web interfaces share a small set of conventions regardless of domain, and a professional human user relies on that prior instead of re-examining every new page from scratch. This pillar gives the agent the same shortcut, keeping routine steps from paying LLM latency at all.

* **Header/Nav Landmark Pass**: A sub-millisecond deterministic synonym matcher scans the A11y tree's structural regions (header/banner, nav, main, footer) against a canonical-intent table:

| Canonical intent | Typical region | Common phrasings / signals |
| :--- | :--- | :--- |
| **Sign up** | header, top-right | Sign up, Register, Create account, Join |
| **Log in** | header, top-right | Log in, Sign in, Member login |
| **Search** | header, top-center | magnifying-glass icon, role="search" |
| **Cart** | header, top-right | cart/bag icon, item-count badge |
| **Cookie/consent banner** | overlay, top or bottom | Accept, Reject, "We use cookies" |
| **Checkout — address step** | main, wizard step 1 | Shipping address, Delivery details |
| **Checkout — payment step** | main, wizard step 2/3 | Card number, Payment method, Billing |

*(This table is kept small on day one and grown from real disagreements between what it predicted and what the Verify step actually found).*

* **Direct-URL Shortcuts**: Before clicking through a nav menu, the agent tries known/inferable URL patterns directly (`/cart`, `/account`, `/checkout`, `/login`) on a previously-seen domain, falling back to UI navigation only on a 404 or unexpected redirect.
* **Dark-Pattern Flag**: Because this agent spends real money on the user's behalf, the landmark pass also flags known manipulative shapes at decision points — most commonly a pre-checked add-on with a price attached, positioned immediately before a payment/submit control, and "guilt-trip" decline copy on discount offers (*"No thanks, I don't want to save money"*). Flagged elements are never auto-accepted as the page defaulted them; they route to the same `is_consequential` path as Pillar 5's Intent-Alignment Critic, requiring an explicit decision rather than "leave it as-is."
* **Impact**: Common navigation steps execute instantly with 0 LLM token cost when confidence is high; low-confidence or novel layouts fall through to the full Cognitive Engine Loop.

### Pillar 4: Macro-Action Batching with Staleness Checks
* **The Problem**: 1 LLM call per single keystroke takes 15–20 seconds for a basic form.
* **The Solution**: The agent emits a batched `MacroAction` (e.g. `[type name, type email, click continue]`).
* **Target Signature Guard**: Every step records a hash of the target element (`role + name + visual_bounds`). If an unexpected modal interrupts the flow mid-batch, the signature fails to match, and the batch aborts at that exact step, preventing blind clicks into incorrect elements.
* **Signature Capture Timing**: `target_signature` is captured only *after* the Visual Stabilization Guard confirms the page is DOM/network-quiescent — never mid-reflow. Capturing it before the page settles produces false-positive staleness aborts.

### Pillar 5: Security, Credential Tokenization & Prompt Injection Defense
* **Credential Isolation**: Secrets (passwords, card CVVs) are NEVER placed in LLM prompt strings or logs. They are referenced as tokens:
  ```python
  MacroStep(action="type", target="f1:e1", value="{{cred:user_payment_card}}")
  ```
  The browser-level runtime resolves tokens securely from Messa's encrypted vault.
* **Indirect Prompt Injection Shield**: Text extracted from untrusted web pages is wrapped in strict delimiters (`<web_content_untrusted>`) with instructions forbidding the execution of embedded commands.
* **Intent-Alignment Critic (Consequential-Action Gate)**: Delimiter-wrapping alone is a known-insufficient defense on its own. Any `MacroAction` flagged `is_consequential=True` (order submission, address change, payment-method addition, account creation) is passed through a second, narrow model call that sees **only** the user's original stated goal and a summary of the proposed action — never the raw page content that produced it — and answers one question: *does this match what the user actually asked for?*
  - **aligned** → proceed to execution.
  - **misaligned/uncertain** → route to Human Checkpoint.
* **Visual/Screenshot Injection Defense**: Once Computer-Use `(x, y)` fallback activates, screenshots become an input surface too. Anything read from a screenshot is treated under the same untrusted-content policy as tree text, and any screenshot-grounded click mapping to a consequential action is routed through the same Intent-Alignment Critic gate.

### Pillar 6: Generalized Human Checkpoints
Human-in-the-Loop is generalized into four distinct checkpoint types:
1. `otp`: SMS 6-digit verification code.
2. `captcha`: If bot defense triggers an interactive puzzle, Messa sends an interactive Live View URL to the user: *"Tap here to solve verification on your screen in 5 seconds."*
3. `payment_3ds`: Bank mobile app approval notification.
4. `risk_review`: High-value cart confirmation (*"Walmart order total is $112.40. Reply YES to confirm purchase."*), or a Pillar 5 Intent-Alignment Critic misaligned/uncertain verdict, or a Pillar 2 non-allowlisted popup.

*(Note: risk_review on value is a legitimate confirmation the user approves; a Critic-triggered risk_review is a silent block of an action that doesn't match the user's stated goal, regardless of dollar value. The SMS copy clearly indicates which trigger occurred).*

### Pillar 7: Reflection, Loop Detection & Circuit Breakers
* **Selective Request Interceptor**: Aborts video streams, tracking scripts (*Criteo, DoubleClick, TikTok, Omniture*), and marketing fonts before download over the residential proxy. Preserves high-res product photos and logos intact for visual fidelity.
* **Bandwidth Savings**: Reduces per-session proxy data from **~12 MB down to ~1.5 MB** (85% reduction).
* **Circuit Breaker Limits**:
  - **Max Steps**: 15 atomic steps per task.
  - **Exact-Repeat Interrupt**: Halts if the same action occurs twice consecutively with zero DOM change.
  - **Oscillation Detector**: Halts on short repeating cycles across the last 6 actions (e.g., A ➔ B ➔ A ➔ B), not just an identical action repeated twice consecutively.
  - **Cost Cap**: $0.35 total LLM budget per task.

### Pillar 8: Continuous Evaluation & Production Drift Monitoring
* **Nightly Regression Suite**: Re-runs every skill blueprint (Walmart cart build, Instacart signup, Google SSO flow) against live sandbox accounts on a fixed schedule, alerting on success-rate or step-count drift.
* **Challenge/Block-Rate Tracking**: Tracks CAPTCHA-trigger and proxy-block rate per site independently of task success rate — a rising challenge rate is an early warning even while tasks are still nominally succeeding.
* **Skill Cache Auto-Invalidation Feed**: A regression failure on a blueprint immediately marks the corresponding Self-Healing Skill Cache entry stale, so the next real user run falls back to full reasoning instead of replaying a broken sequence.

---

## 4. Structured Action Primitives

```python
from typing import List, Literal, Optional, Dict, Any
from pydantic import BaseModel, Field

class MacroStep(BaseModel):
    action: Literal["click", "type", "press_key", "scroll", "select_option", "wait_for"]
    target_id: str                      # e.g., "e3" or "f1:e2"
    target_signature: str               # sha256(role:name:tag), captured AFTER Visual Stabilization Guard confirms quiescence
    value: Optional[str] = None         # text or token (e.g., "{{cred:password}}")

class MacroAction(BaseModel):
    steps: List[MacroStep]
    expect: Dict[str, Any]              # Assertion: e.g., {"url_contains": "/checkout"}
    is_consequential: bool = False      # True routes through IntentAlignmentCheck before execution
                                         # (order submit, address change, payment add, account creation)

class IntentAlignmentCheck(BaseModel):
    """Gatekeeper for consequential actions. Sees ONLY the user's original stated
    goal + a summary of the proposed action — never the raw page content that
    produced it — so an injected instruction upstream can't also fool this check."""
    user_goal: str
    proposed_action_summary: str
    verdict: Literal["aligned", "misaligned", "uncertain"]
    reason: str

class PopupOriginPolicy(BaseModel):
    allowlisted_origins: List[str]      # e.g. ["accounts.google.com", "appleid.apple.com"]
    on_non_allowlisted_popup: Literal["risk_review", "block"] = "risk_review"

class HumanCheckpoint(BaseModel):
    kind: Literal["otp", "captcha", "payment_3ds", "risk_review"]
    prompt_to_user: str                 # SMS message sent to user
    interactive_url: Optional[str] = None  # Live View link if captcha/biometrics needed
    timeout_seconds: int = 300          # Default 5-minute keepalive window
    trigger_reason: Optional[str] = None   # "value_threshold" | "critic_misaligned" | "non_allowlisted_popup" | "captcha"

class AgentDecision(BaseModel):
    status: Literal[
        "OK_CACHED",        # served from Skill Cache, no model call
        "OK_LANDMARK",      # served from the zero-LLM Web Convention Prior
        "OK_REASONED",      # full Cognitive Engine Loop reasoning was used
        "BLOCKED_MODAL",
        "BLOCKED_INJECTION",
        "NEEDS_HUMAN",
        "DONE",
    ]
    thought: Optional[str] = None       # populated ONLY for OK_REASONED / BLOCKED_* / NEEDS_HUMAN
    action: Optional[MacroAction] = None
    checkpoint: Optional[HumanCheckpoint] = None
    result_summary: Optional[str] = None
    scratchpad_updates: Optional[Dict[str, Any]] = None  # persisted to Messa active task artifacts
```

---

## 4.5. Messa Ecosystem Integration: Scratchpad, Skills & Live Stream

Rather than operating in isolation with ephemeral in-memory state, `light-web-agent` is directly integrated into Messa's persistent multi-channel foundation:

### 1. Active Task Scratchpad Bridge (`messa.db` & `messa.tools.scratchpad_tools`)
- **Task Binding**: On initialization with `user_id`, the agent inspects `db.get_active_task(int_user_id)`. If an active task exists, its artifacts (`cart_items`, `credentials`, `stage`, `notes`) are injected into the cognitive reasoning prompt. If none exists, an active task is automatically started via `db.start_active_task`.
- **Progressive Persistence**: Macro action outcomes, cart item additions, and credentials discovered or created during navigation are merged into the task artifacts via `db.update_active_task_artifacts`.
- **Lifecycle Completion**: Upon reaching a `DONE` status, the agent sets the task status to `completed` via `db.set_active_task_status(task_id, 'completed')`.
- **Graceful Fallback**: If Postgres is unavailable (offline test runners or standalone invocations), operations seamlessly fall back to an in-memory dictionary cache without errors.

### 2. Database-Backed Skills Playbook (`db.search_skills` & `db.upsert_skill`)
- **Domain Pre-flight**: Before querying the LLM planner, the agent searches the persistent skills database for domain-specific quirks using `db.search_skills("light_web_agent", domain)`.
- **Automatic Knowledge Capture**: When the agent resolves an obstacle, navigates a complex multi-step checkout pattern, or verifies a repeatable macro sequence, it records the lesson via `db.upsert_skill(user_id, "light_web_agent", domain, pattern, recipe)`.
- **Safety Screening**: All lessons written to the playbook are screened via `_screen_skill_text` to guarantee no sensitive user credentials (`{{cred:...}}` tokens or raw secrets) are ever stored.

### 3. Deepsearch-Style Step Remembrance & Rolling Ledger
- **Episodic Action Ledger**: Maintains a granular history of the last 4-5 actions (selector, element role, target name, value, outcome).
- **Anti-Loop Self-Healing**: In Single Page Applications (e.g. Next.js, React), button clicks frequently trigger client-side loading spinners without page URL changes. The prompt dynamically injects an anti-oscillation guard:
  `[SELF-HEALING GUARD]: If the exact same action was just attempted without a URL or DOM change, DO NOT repeat it. Pivot to an alternate path or wait.`
- **Oscillation Circuit Breaker**: Triple-checks action cycles across the last 6 turns and halts if an infinite click loop is detected.

### 4. Real-Time Live Activity Streaming (`messa.live_activity`)
- **User Live View Feed**: Dispatches real-time events to `messa.live_activity`:
  - `start(user_id, goal)` when launching the browser.
  - `set_session_id(user_id, session_id)` connecting the Browserbase live stream.
  - `set_url(user_id, page.url)` as the page navigates.
  - `set_description(user_id, decision.thought)` updating the current cognitive thought above the video player.
  - `add_step(user_id, step_desc)` updating the live chain-of-thought timeline panel.
  - `set_waiting_for_human(user_id, prompt)` signaling interactive user intervention.
  - `set_closing(user_id)` and `clear(user_id)` on session release.
- **SMS Integration**: Users viewing `/live/<token>` see identical real-time video and thought stream as Deepsearch tasks.

---

## 5. Implementation Roadmap (Branch: `feature/light-web-agent`)

### Phase 1: Core Engine & Persistent Session State Manager
- [x] Create `messa/channels/light_web_agent.py` and `messa/channels/browser_session_manager.py`.
- [x] Implement `SessionManager` storing active CDP connections and page instances.
- [x] Implement 40s heartbeat keepalive pings and 5-minute hard TTL auto-teardown.
- [x] Build `PageStackManager` listening to `context.on("page")` for auto-popup/tab switching, gated by `PopupOriginPolicy`.

### Phase 2: Perception Engine & Network Shield
- [x] Build recursive `extract_pruned_a11y_tree()` traversing top-level DOM, iframes, and Shadow Roots.
- [x] Add `target_signature` generator; wire it to fire only after the Visual Stabilization Guard confirms quiescence.
- [x] Wire request-level media/tracker interceptor for residential proxy bandwidth reduction.
- [x] Build the zero-LLM Web Convention Prior: landmark/synonym table, direct-URL shortcut list, and dark-pattern flag list.

### Phase 3: Cognitive Decision Loop & Reflection Engine
- [x] Implement the `Sense-Reflect-Act-Verify` loop powered by Claude 3.5 Sonnet (with GPT-4o fallback).
- [x] Build obstacle reflection engine (auto-detecting and dismissing promo modals/popups).
- [x] Integrate Circuit Breakers: 15-step cap, exact-repeat detector, oscillation detector (short cycles across last 6 actions), cost tracking.
- [x] Add Computer-Use coordinate visual fallback when A11y trees lack matches, wired into the same injection-defense policy as text content.
- [x] Implement the lean `AgentDecision.status` closed-set output for the fast path; reserve full thought generation for reasoning steps only.

### Phase 4: Macro-Action Batching, Memory & Skills Integration
- [x] Build `MacroExecutor` executing batched actions with mid-flight staleness abortion.
- [x] Implement `CredentialVault` token replacement (`{{cred:...}}` resolved locally).
- [x] Integrate Messa Active Task Scratchpad (`db.get_active_task`, `db.update_active_task_artifacts`).
- [x] Integrate Messa Skills Playbook (`db.search_skills`, `db.upsert_skill`).
- [x] Wire Deepsearch-style step remembrance, rolling ledger, and anti-loop prompt guard.

### Phase 5: Human Checkpoint Relay, Live Activity & Intent-Alignment Critic
- [x] Implement `pause_for_human()` dispatching SMS via Messa's messaging channel.
- [x] Wire real-time `live_activity` streaming (`set_url`, `set_description`, `add_step`, `/live/<token>`).
- [x] Build incoming SMS resume hook to reconnect active sessions and input OTP codes.
- [x] Implement Live View URL generation for manual user CAPTCHA/3DS takeover.
- [x] Implement `IntentAlignmentCheck`: every `MacroAction` with `is_consequential=True` is evaluated against the user's original goal before execution.

### Phase 6: End-to-End Benchmarks
- [x] Benchmark 1: Walmart end-to-end shopping & navigation test on Browserbase.
- [x] Benchmark 2: Instacart live cloud run with macro action execution and live observation view.
- [ ] Benchmark 3: Multi-tab Google SSO login flow, including a deliberately non-allowlisted popup test case.

### Phase 7: Continuous Evaluation & Production Cutover
- [ ] Build the nightly regression scheduler re-running every Phase 6 benchmark against live sandbox accounts.
- [ ] Wire regression failures to auto-invalidate the corresponding Skill Cache entry.
- [ ] Build a challenge/block-rate dashboard per site + proxy pool, alerting independently of task success rate.
- [ ] Deprecate legacy `deepsearch_agent` and route all autonomous browser tasks through `light-web-agent` only once Phase 7 monitoring is live.