# Dynamic Integration Engine & Tool Discovery Architecture (Spec 020)

## Executive Summary
This document specifies the technical design for Messa's **Dynamic Integration Engine** (Migration 020). 

The feature enables Messa to dynamically discover, search, connect, and execute tools across Composio's 1,400+ toolkit catalog on a per-user basis. It requires **zero modifications to Messa's core orchestrator compilation model**, introduces **no pre-turn classification latency**, and enforces strict **approval gating** and **tenant isolation**.

---

## Key Architecture Principles

1. **Model-Decided Tool Discovery (2 Always-On Meta-Tools):**
   Instead of an external pre-turn classifier trying to guess user intent, Messa exposes **2 lightweight always-on meta-tools**:
   - `search_integration_tools(query)`: Searches connected apps (and Composio's catalog) for candidate actions (~150–300 tokens).
   - `execute_integration_tool(slug, arguments)`: Executes the resolved tool via `composio.tools.execute(slug=..., user_id=...)`.

2. **Zero Codebase Rearchitecture (Per-Turn Rebuild):**
   Messa already rebuilds `build_orchestrator` and subagent toolsets fresh on every turn (`cli.py` and `server.py`). This feature is **purely additive** and does not alter the underlying execution graph.

3. **Strict Approval Gating:**
   All write/update/delete operations on connected apps pass through Messa's existing `approval_gate` (pending actions), requiring explicit SMS confirmation before executing destructive tasks (e.g. deleting a Notion page or posting to Slack).

4. **Deterministic Code-Authored OAuth Links:**
   OAuth authorization links for new app connections are returned as code-authored, deterministic text strings (never left to LLM paraphrasing) to ensure zero URL corruption.

5. **Web Automation Fallback with Messa Email:**
   If a requested app is not present in Composio's 1,400+ catalog, Messa falls back to **`deepsearch_agent` (Browserbase + Playwright)**. When registering or signing up for non-API web services, Messa uses the user's assigned personal address (`<username>@textmessa.com`) on paid Browserbase infrastructure.

6. **Preserve Existing Gmail Integration:**
   `messa/tools/email_tools.py` remains unchanged to preserve tested connection polling loops and auth-config caching.

---

## System Workflow & Execution Pipeline

```
           [User Input: "Add 'Prepare report' to my Todoist"]
                                  │
                                  ▼
           Model recognizes no native Messa tool matches
                                  │
                                  ▼
             Calls `search_integration_tools(query="todoist")`
                                  │
                                  ▼
            ┌───────────────────────────────────────────┐
            │ Is app connected for user in Composio?    │
            └───────────────────────────────────────────┘
                                 / \
                           YES  /   \ NO
                               /     \
                              ▼       ▼
                  Returns tool     Returns 1-Click Code-Authored
                  candidate slugs  OAuth Link to User
                              │
                              ▼
                  Model calls `execute_integration_tool`
                              │
                              ▼
               ┌──────────────────────────────┐
               │ Is action a WRITE/DESTRUCTIVE?│
               └──────────────────────────────┘
                                 / \
                           YES  /   \ NO (Read-only)
                               /     \
                              ▼       ▼
                  Route to `approval_gate`   Execute immediately via
                  (Sends SMS confirmation)    `composio.tools.execute`
```

---

## Detailed Component Specifications

### 1. `search_integration_tools(query: str)`
- **Functionality:** Calls `composio.tools.get(search=query, user_id=user_id)`.
- **Filtering Logic:** First checks if the tool belongs to an app the user has already connected. If not connected, returns the app's metadata along with a deterministic code-authored OAuth authorization link.
- **Token Footprint:** ~150–300 tokens max per search turn.

### 2. `execute_integration_tool(slug: str, arguments: dict)`
- **Functionality:** Calls `composio.tools.execute(slug=slug, user_id=user_id, arguments=arguments)`.
- **Safety Gate:** Evaluates the action slug. If the action modifies state (create, update, delete, post, transfer), it creates a pending action row in `pending_actions` and sends an SMS approval text to the user before running.

### 3. Non-API Web Automation Fallback (Browserbase + Messa Email)
- If `search_integration_tools` yields no API integration, Messa routes to `deepsearch_agent` running on paid Browserbase infrastructure.
- If creating an account on the target web service is required, Messa signs up using the user's assigned address (`<username>@textmessa.com`).

---

## Database Migration Design (`migrations/020_user_app_preferences.sql`)

```sql
-- User's App Category Preferences (e.g. preferred email/task provider)
CREATE TABLE IF NOT EXISTS user_app_preferences (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    app_category VARCHAR(64) NOT NULL, -- e.g. 'email', 'tasks', 'calendar'
    preferred_app VARCHAR(64) NOT NULL, -- e.g. 'todoist', 'gmail', 'messa'
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id, app_category)
);

CREATE INDEX idx_user_app_preferences_user ON user_app_preferences(user_id);

-- Log of Unsupported App Requests for Product Roadmap & Developer Review
CREATE TABLE IF NOT EXISTS unsupported_integration_requests (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_app_name VARCHAR(128) NOT NULL,
    raw_user_prompt TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_unsupported_requests_app ON unsupported_integration_requests(requested_app_name);
```

---

## Summary of Design Fixes & Advantages

1. **No Pre-Turn Classifier:** Eliminates misclassification latency by letting the LLM decide when to call `search_integration_tools`.
2. **Approval Gate Protected:** All 3rd-party write actions require explicit user confirmation via SMS.
3. **Deterministic OAuth Links:** Prevents model URL hallucinated links.
4. **Paid Browserbase & Messa Email Account Setup:** Uses `<username>@textmessa.com` for web signups on Browserbase.
5. **Zero Gmail Churn:** Keeps existing `messa/tools/email_tools.py` working untouched.
