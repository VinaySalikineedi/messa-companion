# Orchestrator Routing Hierarchy & Subagent Boundaries Specification

## Objective
This specification defines the routing rules and subagent tool boundaries for Messa's top-level orchestrator (`messa/agents/registry.py`). 

The goal is to ensure **100% automatic, deterministic routing** for user requests, preventing misrouting (e.g., sending Google Calendar connect requests to reminders or web-scraping Reddit instead of using authenticated Composio APIs).

---

## 3-Tier Routing Decision Tree

```
                          ┌──────────────────────────┐
                          │    Incoming User Prompt  │
                          └──────────────────────────┘
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            ▼                          ▼                          ▼
  Explicit Web Search        App / Platform Request        Calendar & Schedule
  (e.g., "Google Paris       (e.g., "Check Reddit",        (e.g., "Connect GCal",
  weather", "Find recipe")   "Post to Slack", "Todoist")   "Schedule meeting")
            │                          │                          │
            ▼                          ▼                          ▼
      `web_search`             `integrations_agent`      `executive_assistant`
            │                   (Composio 1400+ Apps)      (Reminders + GCal)
            ▼                          │
      If blocked/deep            If app/tool missing:
      topic, use                 Fallback to `web_search`
      `deepsearch`               / `deepsearch`
```

---

## Subagent Boundaries & Required Changes

### 1. `executive_assistant` (Core Calendar & Schedule Management)
- **Boundary:** Handles all scheduling, reminders, daily briefings, AND Google Calendar operations (both connecting and managing events).
- **Update Required:** Add Google Calendar tools (`GOOGLECALENDAR_*`) directly to `executive_assistant`'s available skills, matching how `email_agent` handles both internal Messa email and Gmail.

### 2. `integrations_agent` (3rd-Party Apps & Services via Composio)
- **Boundary:** Handles all 3rd-party apps and popular web platforms (Reddit, Slack, Notion, Todoist, Instagram, GitHub, Trello, etc.).
- **Routing Rule:** Any prompt requesting data, posts, or actions on a known app/platform MUST route to `integrations_agent` **first**.
- **Fallback Rule:** If `search_integration_tools` yields no Composio tool match, `integrations_agent` falls back to `web_search` or `deepsearch` browser automation.

### 3. Direct Web Search (`web_search` & `deepsearch`)
- **Boundary:** Handles explicit general web queries (e.g., *"Search the web for Paris weather"*, *"Who won the game last night?"*, *"Find a recipe for lasagna"*).
- **Routing Rule:** Route directly to `web_search` / `deepsearch` — do **not** check Composio for generic web search prompts.

---

## Technical Action Items for Developer (`messa/agents/registry.py`)

1. **Update `executive_assistant` Prompt & Tools:**
   - Include Google Calendar management in `executive_assistant` description and toolset.
2. **Update Orchestrator Router Prompt:**
   - Explicitly instruct the router: *"For popular apps/platforms (Reddit, Slack, Todoist, Notion, etc.), delegate to `integrations_agent` first. For explicit web searches, call `web_search` directly."*
