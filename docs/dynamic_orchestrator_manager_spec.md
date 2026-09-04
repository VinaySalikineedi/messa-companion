# Dynamic Multi-Agent Orchestrator & OTP Resolution Specification

## 1. Objective & Problem Statement
Currently, Messa acts as a **fire-and-forget router**: it receives a user request, delegates to a subagent (`deepsearch`, `email_agent`, etc.), and waits for it to return text before concluding the turn.

During complex multi-step tasks (such as account creation on Uber Eats), this isolated model fails:
1. **Premature Session Teardown:** When Deepsearch reaches an email OTP/verification screen, it has no direct line back to Messa or Messa's email tools. It exhausts its tool retries, exits, and destroys the active Browserbase browser session.
2. **Disconnected Inbound Webhook:** When the verification email arrives at `/webhooks/personal-email/inbound`, the server treats it as an independent incoming message and starts a new SMS turn with the user, rather than routing the code into the waiting browser.
3. **Session Loss on Resume:** Subsequent attempts to resume open a blank tab (`about:blank`) or redirect to a landing page, completely losing the modal state.
4. **Zero-Action Hallucination:** Under reasoning models (`o3-mini`), when Deepsearch outputs conversational text without invoking browser tools, the lazy-opening mechanism never starts a browser session (`run finished without ever opening a browser session`), yet marks the task `completed`. Messa then reports fabricated success to the user.
5. **Orchestrator Dropped Delegations:** Prompts instructing Messa to "send an acknowledgment before delegating" cause models like `o3-mini` to send text and stop without issuing a tool call.

This specification outlines the architecture to make Messa an **active, collaborative manager** that coordinates workers, maintains live browser state, and resolves dependencies autonomously.

---

## 2. Target Architecture: The Manager-Worker Collaboration Loop

```
                     ┌────────────────────────────────────────┐
                     │          User Request via SMS          │
                     │ ("Create Uber Eats account for Vinay") │
                     └───────────────────┬────────────────────┘
                                         ▼
                     ┌────────────────────────────────────────┐
                     │         Messa (The Orchestrator)       │
                     │ - Initializes Mission & Shared Context │
                     │ - Delegates initial browser task       │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │          Deepsearch (Worker)           │
                     │ - Navigates to ubereats.com            │
                     │ - Enters email -> Reaches OTP screen   │
                     │ - HOLDS BROWSER WARM IN MEMORY         │
                     │ - Calls: `ask_manager(need="otp")`     │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │      Active Expectation Registry       │
                     │ Registered: {sender: "uber",           │
                     │              session_id: 91,           │
                     │              user: vinay@textmessa.com}│
                     └───────────────────┬────────────────────┘
                                         │
        Inbound Email Webhook            │
    ┌───────────────────────────┐        │
    │  POST /webhooks/personal- │        │
    │  email/inbound (Code 2573)│───────►│  Matched expectation!
    └───────────────────────────┘        │  Bypasses normal SMS notification
                                         ▼
                     ┌────────────────────────────────────────┐
                     │        Direct In-Session Resume        │
                     │ - Injects code 2573 into session #91   │
                     │ - Fills OTP inputs & clicks submit     │
                     │ - Verifies success on DOM              │
                     └───────────────────┬────────────────────┘
                                         │
                                         ▼
                     ┌────────────────────────────────────────┐
                     │              Messa to User             │
                     │ "Uber Eats account created & verified!"│
                     └────────────────────────────────────────┘
```

---

## 3. Five Core Pillars of the Plan

### Pillar 1: Subagent Suspension & Back-Channel (`ask_manager` / `await_email_verification_code`)
* **Problem:** Deepsearch cannot talk to Messa while running; it can only finish or crash.
* **Fix:** Add a dedicated coordination tool to `deepsearch_tools.py`:
  - `await_email_verification_code(sender_keyword="uber", timeout_seconds=90)`
  - When invoked on a verification screen, Deepsearch registers an expectation in memory/database and enters an active polling loop (identical to `_request_human_help`'s wait loop).
  - **Crucial:** The browser tab and Browserbase session remain live and warm.
  - The moment the inbound email webhook logs the message, the tool immediately returns the parsed OTP code (e.g. `"2573"`) directly into the active prompt turn. Deepsearch types the code and continues seamlessly.

### Pillar 2: Inbound Webhook Event Dispatcher
* **Problem:** Inbound emails currently spawn an uncoordinated top-level SMS chat prompt via `_process_inbound_personal_email`.
* **Fix in `server.py`:**
  - Before spawning a general user notification, check `db.get_active_otp_expectations(user_id, sender)`.
  - If a matching expectation exists:
    1. Parse the numeric OTP code (`\b\d{4,6}\b`).
    2. Mark the expectation resolved in DB.
    3. The waiting poller in Deepsearch receives the code within <1 second.
    4. Suppress the intrusive SMS to the user so the process feels fully autonomous.

### Pillar 3: Fix `browser_snapshot` Permission Error (`EACCES`)
* **Problem:** In Docker / HF Spaces, Playwright MCP fails when the LLM specifies `filename="ubereats_signup_page_snapshot.md"` because `/app` is read-only. This blinded the agent on every turn.
* **Fix in `deepsearch_tools.py`:**
  - In `BrowserToolProvider.call_tool("browser_snapshot", args)`:
    - If `filename` is provided, strip it or rewrite it to `/tmp/<filename>`.
    - Without a relative path to `/app`, Playwright MCP returns the snapshot markdown in-memory, ensuring 100% snapshot reliability.

### Pillar 4: Anti-Hallucination Execution Gate
* **Problem:** Reasoning models (`o3-mini`) can generate plausible conversational text without calling browser tools. Deepsearch marks this `status="completed"`, and Messa assumes verified truth.
* **Fix in `deepsearch_tools.py` (`_run`):**
  - If a browsing task finishes with `status == "completed"`, but `provider._live_session_ready` is `False` (zero browser actions executed) and no web reader tool was used:
    - Reject the completion.
    - Inject a corrective prompt: *"You reported task completion without performing any browser navigation or actions. Execute the required browser tools now."*
    - If it still fails, report an honest failure instead of claiming success.

### Pillar 5: LangGraph Message Sanitization (Prevent OpenAI 400 Errors)
* **Problem:** Unhandled exceptions inside tool execution cause LangGraph to terminate without a `ToolMessage` for the pending `tool_call_id`. Resuming passes an orphaned tool call to OpenAI, causing `Error 400: No tool output found for function call`.
* **Fix in `deepsearch_tools.py` & `cli.py`:**
  - Introduce `sanitize_tool_messages(messages)` before saving to DB or sending to OpenAI.
  - Scans for any `AIMessage` with `tool_calls`. If the following message is not a `ToolMessage` with matching IDs, it automatically appends synthetic `ToolMessage(content="Action interrupted", tool_call_id=...)`.

---

## 4. Code Wiring & Implementation Details

### 1. `messa/db.py` (New Expectation Table)
```sql
CREATE TABLE IF NOT EXISTS active_agent_expectations (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    expectation_type VARCHAR(64) NOT NULL, -- 'email_otp'
    sender_filter VARCHAR(128),            -- 'uber'
    code VARCHAR(32),
    status VARCHAR(32) DEFAULT 'pending',  -- 'pending', 'resolved', 'expired'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    resolved_at TIMESTAMP WITH TIME ZONE
);
```

### 2. `messa/tools/deepsearch_tools.py`
* Add `await_email_verification_code` to `BrowserToolProvider.tools`:
  - Registers row in `active_agent_expectations`.
  - Polls `db.check_agent_expectation(expectation_id)` every 2s for up to 90s while maintaining the browser tab.
  - Returns: `"RESOLVED: Received verification code 2573 from Uber."`
* Sanitize `browser_snapshot` arguments in `guarded()` to prevent `EACCES`.
* Add execution validation gate in `_run()` before returning `status="completed"`.

### 3. `messa/server.py`
* In `personal_email_inbound_webhook` / `_process_inbound_personal_email`:
  - Before generating a generic user notification, query `db.find_and_resolve_agent_expectation(user_id, from_address, body_text)`.
  - If resolved, update the table so Deepsearch receives the code immediately.

### 4. `messa/cli.py`
* Expand `_STALL_PATTERN` regex to capture future-tense promises (`Opening...`, `Fresh browser...`, `I will navigate...`, `Heading to...`).
* If Messa outputs an action promise with zero tool calls, `run_turn` catches it and forces the delegation through.
* Apply `sanitize_tool_messages()` on message loading and resumption.

---

## 5. Verification Plan

1. **Unit Test - `browser_snapshot` EACCES Fix:**
   - Execute `browser_snapshot` with relative filenames (`test.md`); verify it redirects to `/tmp/` and returns markdown content cleanly.
2. **Unit Test - LangGraph 400 Sanitizer:**
   - Inject an orphaned `AIMessage(tool_calls=[...])` without a `ToolMessage`; verify `sanitize_tool_messages` heals the list and OpenAI accepts it without error 400.
3. **Integration Test - OTP Loop:**
   - Run Deepsearch to an OTP wait state.
   - Trigger a simulated inbound webhook from `uber.com` with code `9812`.
   - Verify Deepsearch receives `9812` in the same session without user SMS intervention.
4. **Integration Test - Anti-Hallucination Gate:**
   - Force an agent turn that returns text without calling tools; verify the runner rejects the text and prompts for real execution.
