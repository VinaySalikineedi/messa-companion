# `light-web-agent`: Architecture & Implementation Specification

**Project**: Messa Autonomous AI Concierge  
**Target Component**: `light-web-agent` (Universal Dynamic Web Navigation Agent)  
**Status**: Architecture Review & Planning  
**Replaces**: Brittle multi-agent web crawler tree (`deepsearch_agent`)  

---

## 1. Executive Summary & Problem Statement

### The Problem with Existing Multi-Agent Web Systems
1. **The Stateless Browser Teardown Antipattern (The OTP Infinite Loop)**:
   - Traditional web sub-agents execute as one-shot functions: `run_task() ➔ close_browser()`.
   - When hitting a sign-up/login verification screen (OTP / 2FA), the sub-agent exits and kills the browser.
   - When the user sends the OTP over SMS, the system spawns a *brand-new* browser with empty cookies on a blank tab. The active sign-up form is lost, prompting a *new* OTP, rendering the user's code obsolete.
2. **Brittle Rigid Selectors vs. Real-World Web Dynamism**:
   - Predetermined sub-agents expect a rigid sequence (`Click A ➔ Type B ➔ Click C`).
   - If an unexpected modal or promotion appears (e.g. *"$0 delivery fee for new users"*, cookie banners, loyalty popups), hardcoded steps crash.
3. **Heavy Token & Bandwidth Bloat**:
   - Injecting raw HTML (100k+ lines) into the LLM context window causes hallucination and high latency.
   - Unrestricted asset downloads (video ads, tracking pixels) quickly exhaust metered residential proxy bandwidth.

---

## 2. Core Architectural Pillars of `light-web-agent`

`light-web-agent` replaces complex multi-agent handoffs with a **single, persistent, dynamic cognitive loop**.

```
                           +------------------------+
                           | User Intent (via SMS)  |
                           +-----------+------------+
                                       |
                                       v
                     +-----------------+------------------+
                     | Messa Skill Blueprint Registry     |
                     | (e.g., Walmart, Instacart Playbook)|
                     +-----------------+------------------+
                                       |
                                       v
                  +--------------------+--------------------+
                  |       THE COGNITIVE SENSE-ACT LOOP      |
                  |                                         |
                  | 1. PERCEIVE: Pruned A11y Tree (<1.5k tok)
                  | 2. REFLECT: Detect modals, popups, OTP   |
                  | 3. ACT: Stagehand Primitives (Click/Type)|
                  | 4. VERIFY: Did DOM state change?        |
                  +--------+-----------------------+--------+
                           |                       |
                 Normal Action Steps          OTP / 2FA Detected
                           |                       |
                           v                       v
                  +--------+--------+    +---------+----------+
                  | Cloud Browser   |    | Suspended State    |
                  | (Browserbase /  |    | - Keepalive ping   |
                  |  Residential)   |    | - SMS sent to user |
                  +-----------------+    | - Await user reply |
                                         +---------+----------+
                                                   |
                                            User Texts Code
                                                   |
                                                   v
                                         +---------+----------+
                                         | Resume ACTIVE tab  |
                                         | & submit OTP       |
                                         +--------------------+
```

### Pillar 1: Persistent Session Handoff (Zero-Kill OTP)
* **Session-as-State**: The cloud browser session ID (`session_id`) is attached to the user's active session state in memory/Redis, **not** scoped to a single Python function.
* **Human-in-the-Loop Suspension**:
  - When an OTP screen is encountered, `light-web-agent` triggers `pause_for_human(prompt, timeout=300)`.
  - The browser tab **remains open and active** in the cloud with lightweight keep-alive pings.
  - An SMS is dispatched to the user: *"Walmart just texted a verification code to your phone. What is it?"*
  - The LLM execution thread yields ($0 token waste while waiting).
  - When the user texts back the 6 digits, the webhook passes the input directly back into the waiting agent, which reconnects over CDP to the **exact same open tab** and enters the code.

### Pillar 2: Perception Engine (Pruned Accessibility Tree)
* Rather than feeding raw HTML or relying purely on heavy vision screenshots, the agent parses the **Chrome Accessibility (a11y) Tree**.
* **Preprocessing Filter**:
  - Strips SVGs, inline scripts, styles, hidden/invisible containers.
  - Assigns compact semantic indices: `[e1]`, `[e2]`, `[e3]`.
  - Output format:
    ```json
    [
      {"id": "e1", "role": "button", "name": "Claim $0 Delivery Fee"},
      {"id": "e2", "role": "textbox", "name": "Email Address", "value": ""},
      {"id": "e3", "role": "button", "name": "Continue"}
    ]
    ```
* **Context Footprint**: Reduces input from 50,000 tokens down to **< 1,500 tokens**, resulting in sub-second inference and razor-sharp accuracy.

### Pillar 3: Self-Healing Cognitive Loop (Grok / Stagehand Style)
Every cycle follows a 4-step loop:
1. **Observe**: Ingest current pruned a11y state.
2. **Reflect & Critic**:
   - Compare current state to the ultimate goal and the last action taken.
   - **Anomaly Detection**: Is a modal or promotional banner blocking the target form? (e.g. *"$0 delivery fee"*).
   - If blocked: prioritize dismissing or claiming the modal **before** continuing the primary goal.
3. **Act**: Execute atomic actions via high-level primitives:
   - `act("click [e1]")`
   - `act("type 'user@example.com' into [e2]")`
   - `act("scroll down")`
   - `act("press Enter")`
4. **Verify**: Check if the action successfully mutated the page. If the page remained identical, reflect and self-correct (re-focus, scroll into view, or use keyboard navigation).

### Pillar 4: Native Messa Skills Integration
* The agent does not blindly guess when handling known domains.
* When a task begins (e.g., *"Build grocery cart on Walmart"*), the agent inspects the **Messa Skill Registry**:
  - **Skill Loaded**: Uses domain blueprints (target URLs, known input selectors, checkout flows, guest APIs).
  - **Dynamic Adaptation**: If Walmart changes its layout or throws an unexpected promotion, the dynamic Reflector seamlessly takes over, clears the hurdle, and guides the browser back to the skill blueprint.
  - **Fallback**: If no skill exists, the agent operates in pure autonomous exploration mode.

### Pillar 5: Network & Proxy Shield
* **Residential Routing**: Connected to US residential proxies to bypass PerimeterX / Cloudflare bot barriers.
* **Ad & Tracker Interceptor**: Aborts video ads, third-party analytics (Criteo, DoubleClick, TikTok pixels), and heavy marketing fonts before download.
* **Result**: Preserves full visual fidelity (product photos and logos intact for screenshots/Live View) while cutting bandwidth consumption by **60%–70%**.

---

## 3. High-Level Action Primitives

`light-web-agent` exposes 5 core primitives to the LLM:

```python
class LightWebAction:
    action: Literal["navigate", "act", "extract", "ask_human", "complete"]
    target_id: Optional[str] = None       # e.g., "e12"
    value: Optional[str] = None           # text to type, or URL
    human_prompt: Optional[str] = None    # message to text user if asking for OTP
    result_data: Optional[dict] = None    # structured data extracted from page
```

---

## 4. Implementation Roadmap (Branch: `feature/light-web-agent`)

### Phase 1: Core Engine & Persistent Session State
- [ ] Create `messa/channels/light_web_agent.py`.
- [ ] Implement `SessionManager` that stores and retrieves active cloud CDP connections across multiple turns.
- [ ] Add keep-alive pings for paused browser sessions (5-minute window).

### Phase 2: Perception & DOM Pruning
- [ ] Build `extract_a11y_tree(page)` to generate concise, indexed interactive nodes.
- [ ] Integrate request-intercept ad/tracker blocking to maintain low proxy bandwidth.

### Phase 3: Cognitive Loop & Reflection Engine
- [ ] Wire LLM decision loop (Claude 3.5 Sonnet / GPT-4o).
- [ ] Implement obstacle reflection (auto-detecting and dismissing modals/popups).
- [ ] Add self-correction logic when actions fail to trigger state changes.

### Phase 4: Messa Skill Blueprint Hook & Human OTP Relay
- [ ] Expose `ask_human` tool that fires an SMS webhook and suspends the agent loop.
- [ ] Connect Messa Skill Registry (Walmart cart staging, Instacart signup).
- [ ] Build resume handler for incoming OTP text messages.

### Phase 5: Verification & Production Rollout
- [ ] End-to-end benchmark on Walmart signup with live SMS OTP verification.
- [ ] End-to-end benchmark on Instacart account creation.
- [ ] Deprecate `deepsearch_agent` and route all autonomous browser tasks through `light-web-agent`.

---

## 5. Review Prompts for Claude

When sharing this document with Claude, consider asking:
1. *Does the session suspension model have any edge cases during network drops or webhooks delays, and how best should reconnect timeouts be handled?*
2. *Is an indexed accessibility tree sufficient for 95% of e-commerce interactions, or should we pair it with low-resolution visual grounding (Set-of-Mark / coordinate bounding boxes)?*
3. *What is the most token-efficient prompt structure for the Reflection/Critic step to prevent the agent from over-analyzing static pages?*
