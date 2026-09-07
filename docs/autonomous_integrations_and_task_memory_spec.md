# Autonomous Integrations Agent, Active Task Memory & Self-Learning Skills Specification

## Executive Summary
During a live fundraising outreach workflow (researching VCs, creating a Google Sheet, and emailing pitch drafts), Messa and the `integrations_agent` encountered multiple operational failures:
1. **Amnesia**: Messa forgot her own pre-written pitch draft due to aggressive message history truncation (`limit=12`), forcing the user to re-paste it.
2. **Repeated Tool Failures (15+ 400/404 Errors)**: `integrations_agent` attempted invalid Google Sheets tool calls repeatedly because it was not provided the `spreadsheet_id` and hallucinated parameter schemas.
3. **Failure to Prompt for Google Drive**: The agent realized finding files by name required `googledrive` (which was `NOT CONNECTED`), but failed to ask the user to connect it.
4. **Context Overflow**: Email searches returned raw, uncompacted email payloads that blew past model context limits.
5. **Plumbing Leaks**: Technical error messages (*"my Sheets read keeps glitching on my end"*) were texted directly to the user.

This document outlines the architecture to make `integrations_agent` **autonomous, self-correcting, and self-learning**, introduces a **Durable Active Task Scratchpad**, and establishes a **Persistent Skills/Playbook Memory** so the agent compounds intelligence across task repetitions.

---

## Part 1: Failure Analysis (What Happened & Why)

### 1.1 The `limit=12` Context Amnesia Bug
- **Failure**: In turn 1, Messa drafted a complete pitch email (`Subject: Messa — an AI assistant that actually gets work done...`). A few turns later, Messa claimed she had no pitch copy, demanded the user paste it from scratch, and asked what email address to use.
- **Root Cause**: [`messa/cli.py`](file:///Users/robocafedesktop/Documents/textMessa/messa/cli.py#L613) hardcodes:
  ```python
  recent = await db.get_recent_messages(user.user_id, limit=12)
  ```
  Because Messa's responses are split into 2–4 SMS bubbles (each stored as its own database row), 12 rows represents only **2 to 3 back-and-forth turns**. By the time the user replied *"Yes included seven seven six and send emails"*, the pitch draft from Message 1858 was 30 rows back and completely invisible to the LLM.

### 1.2 Tool Calling Loops & Dropped `spreadsheet_id`
- **Failure**: `integrations_agent` made 26 tool calls, over 15 of which failed with `400 Bad Request` or `404 Not Found`.
- **Root Cause**:
  1. In Turn 1, Messa created the spreadsheet and received `spreadsheet_id: "10pEOAkS4SQh_BInwty4d21_6hI2Cn6M_V2cDuYf03zY"`.
  2. In Turn 2, Messa delegated to `integrations_agent`: *"Read the user's connected Google Sheet titled 'Messa — Pre-Seed Investor Targets'"*, **omitting the spreadsheet ID**.
  3. Composio's Google Sheets integration does not support title searching. It strictly requires `spreadsheet_id`.
  4. Lacking the ID, the agent hallucinated invalid arguments across 15+ calls:
     - `{"spreadsheet_name": "Messa — Pre-Seed Investor Targets"}` (400 Missing `spreadsheet_id`)
     - Passing title into `spreadsheet_id` (400 Invalid ID format)
     - Passing wildcard `spreadsheet_id: "*"` (404 Not found)
     - `GOOGLESHEETS_LOOKUP_SPREADSHEET_ROW` with `lookup_value` instead of `query` (400 Missing `query`)

### 1.3 Unconnected App Stalling (Google Drive Miss)
- **Failure**: The agent identified that file searching required `GOOGLEDRIVE_FIND_FILE`, saw it was `[googledrive, NOT CONNECTED]`, but failed to prompt the user to connect it.
- **Root Cause**: The agent had no reflexive escalation rule: when a required capability belongs to an unconnected toolkit, stop trying broken alternatives and immediately trigger `composio_connect_link(toolkit)`.

### 1.4 Raw Email Payload Context Explosion
- **Failure**: When checking the Gmail sent folder, `list_recent_emails` was called with `max_results: 20`.
- **Root Cause**: The tool returned raw MIME bodies, thread histories, and attachment hashes, resulting in an immediate 400 error:
  `The request exceeded GLM-5.3-Flash's context window (max_model_len=1048576)`.

### 1.5 Leaking Internal Plumbing over SMS
- **Failure**: Messa texted the user: *"my Sheets read keeps glitching on my end, and since I won't guess or invent VC inboxes..."*
- **Root Cause**: Absence of an output filter/guardrail separating internal diagnostic errors from customer-facing dialogue.

---

## Part 2: Architectural Solutions

```
                               ┌───────────────────────────────────────────────┐
                               │             USER (SMS / iMessage)             │
                               └──────────────────────┬────────────────────────┘
                                                      │ Inbound Turn
                                                      ▼
 ┌───────────────────────────┐         ┌───────────────────────────────┐
 │   Skills / Tool Playbook  │◄───────►│  Messa Orchestrator (System)  │
 │  (Learned Patterns/Fixes) │         └──────────────┬────────────────┘
 └───────────────────────────┘                        │
                                                      ├─────────────────────────────────┐
                                                      ▼                                 ▼
                                       ┌───────────────────────────────┐ ┌───────────────────────────────┐
                                       │   Active Task Scratchpad      │ │    Turn-Based Context Buffer  │
                                       │ (PostgreSQL: Task State/Assets)│ │ (10 Full Turns + Compaction)  │
                                       └──────────────┬────────────────┘ └───────────────────────────────┘
                                                      │ Injected into subagents
                                                      ▼
                                       ┌───────────────────────────────┐
                                       │     integrations_agent        │
                                       │  (Composio 1,400+ Toolkits)   │
                                       └──────────────┬────────────────┘
                                                      │
                                                      ▼
                        ┌─────────────────────────────────────────────────────────────┐
                        │             Self-Correction & Thinking Loop                 │
                        │ 1. Inspect Error (e.g. 400 Missing spreadsheet_id)         │
                        │ 2. Pull correct parameter from Task Scratchpad              │
                        │ 3. If schema ambiguous: search_web / doc lookup             │
                        │ 4. If app unconnected: generate 1-click connect link        │
                        │ 5. Save working pattern to Skills / Tool Playbook           │
                        └─────────────────────────────────────────────────────────────┘
```

---

## 3. Specification Requirements

### 3.1 Durable "Active Task Scratchpad" (Working Memory)
When a user initiates an action-oriented, multi-turn workflow (e.g., outreach, bookings, research, document creation), Messa must maintain a dedicated active task record in PostgreSQL:

1. **Schema (`active_tasks` table)**:
   - `task_id`: UUID
   - `user_id`: Integer
   - `task_type`: String (e.g., `investor_outreach`, `travel_booking`)
   - `status`: String (`in_progress`, `waiting_user_input`, `completed`, `failed`)
   - `artifacts`: JSONB store of structured entities:
     - `spreadsheet_id`: `"10pEOAkS4SQh_BInwty4d21_6hI2Cn6M_V2cDuYf03zY"`
     - `spreadsheet_url`: `"https://docs.google.com/spreadsheets/d/10pEOAkS4..."`
     - `pitch_draft`: String containing the verified pitch body
     - `recipients`: List of verified contact emails and firms
     - `sender_email`: `"vinay@textmessa.com"`
2. **Injection Protocol**:
   - As long as `status = 'in_progress'`, this `artifacts` dictionary is injected directly into the system prompt of Messa and all subagents.
   - Even if raw chat messages roll off the chat window, the active task assets remain 100% accessible to every turn.

---

### 3.2 Turn-Based Context Window (Fixing `limit=12`)
- **Replace Row-Based Truncation**: Replace `db.get_recent_messages(limit=12)` with a turn-aware query:
  - Retrieve the last **10 complete conversation turns** (all user messages and their corresponding assistant replies), or up to 40 message rows.
  - Apply automatic text compaction to tool executions and older assistant messages to keep token usage lean while preserving conversational continuity.

---

### 3.3 Super-Smart, Self-Fixing `integrations_agent`
Because Composio provides access to 1,400+ tools whose documentation cannot be statically hardcoded, the agent must be dynamically adaptable:

#### A. The Error-Reflection / Thinking Loop
When an integration tool returns an error (HTTP 400/404/422):
1. **Analyze Error Payload**: The agent reads the error details (e.g., `Missing required parameter: spreadsheet_id` or `Field 'query' is required`).
2. **Immediate Remediation**:
   - Check if the missing value exists in the Active Task Scratchpad or earlier tool outputs.
   - Correct the parameter structure immediately rather than executing repeated identical calls.
3. **Circuit Breaker**: Cap identical failed parameter retries at 2. If a tool fails twice with the same signature, pause and enter Fallback Mode.

#### B. Dynamic Web & Documentation Lookup
- Equip `integrations_agent` with access to `search_web` / doc lookup.
- If a Composio action schema is non-obvious or throws unexpected format errors (e.g., whether a tool accepts SQL, A1 range notation, or filter objects), the agent queries Composio's API reference or Google API documentation to retrieve the exact parameter format dynamically.

#### C. Proactive 1-Click Connection Escalation
- If an operation requires a tool whose status is `NOT CONNECTED` (e.g., `googledrive` when resolving spreadsheets by title):
  - **Rule**: Do not attempt to force unrelated tools (e.g., SQL queries on `googlesheets`) to perform actions they cannot support.
  - Automatically call `get_connect_link(toolkit)` and return a friendly, 1-click authorization message:
    > *"To search your Google Sheets by name, I just need access to your Google Drive. Tap here to connect in one tap: [Connect Google Drive Link]"*

---

### 3.4 Persistent Skills & Tool Experience Playbook
To ensure Messa gets smarter with every repetition:

1. **Storage (`integration_skills` / `agent_playbooks`)**:
   - Maintain a persistent knowledge store in PostgreSQL / Vector memory storing operational recipes and fixes:
     - `toolkit`: String (e.g., `googlesheets`, `gmail`, `slack`)
     - `problem_pattern`: String (e.g., `lookup_sheet_by_name_without_drive`, `read_sheet_rows`)
     - `solution_recipe`: String (e.g., *"Cannot search Google Sheets by title without Google Drive. Always require or store `spreadsheet_id`. To read rows, use `GOOGLESHEETS_VALUES_GET` with range `Sheet1!A1:Z100`."*)
     - `success_count`: Integer
2. **Auto-Learning Flow**:
   - When the agent resolves a tricky tool interaction or fixes a 400 error, it extracts a concise 1–2 sentence lesson and persists it.
3. **Retrieval**:
   - Before `integrations_agent` executes calls on a toolkit, it retrieves the relevant skills from the playbook for that toolkit.
   - Future turns and tasks benefit immediately from past lessons, eliminating redundant debugging cycles.

---

### 3.5 Payload Truncation & Professional Persona Guardrails

1. **Email / Search Payload Compaction**:
   - Wrap tools like `list_recent_emails` so they return only `id`, `sender`, `subject`, `date`, and a 200-character `snippet`—never full raw MIME bodies or base64 attachment payloads—preventing context window blowups.
2. **Zero Technical Plumbing Leaks**:
   - System prompt guardrail: *“Never expose API error codes, tool timeouts, or technical plumbing to the user (e.g., 'my Sheets read is glitching'). If a task encounters an unresolvable blocker, ask a professional, concise executive question.”*

---

## 4. Summary of Developer Action Items

| Component | Target File | Action Item |
| :--- | :--- | :--- |
| **Active Task Scratchpad** | `messa/db.py`, `messa/server.py` | Create `active_tasks` table and inject active artifacts into orchestrator/subagent system prompts. |
| **History Window** | `messa/cli.py` | Replace `limit=12` row limit with turn-aware retrieval (10 full user-agent interaction pairs). |
| **Integrations Reflection** | `messa/agents/registry.py` | Implement self-correction thinking loop on tool error payloads + give `search_web` fallback to `integrations_agent`. |
| **Proactive Connect** | `messa/agents/registry.py` | Add reflexive rule: emit 1-click connect link immediately when a candidate tool is `NOT CONNECTED`. |
| **Skills Playbook** | `messa/memory.py` / `messa/db.py` | Implement tool skill recording and retrieval for Composio integrations. |
| **Email Compaction** | `messa/tools/email_tools.py` | Strip raw email bodies and attachment payloads from `list_recent_emails`. |
| **Persona Filter** | `messa/agents/registry.py` | Enforce zero leakage of internal tool error messages to user-facing SMS. |
