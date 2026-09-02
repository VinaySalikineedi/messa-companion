# Dynamic Connected-Apps & Universal Routing Specification

## Objective
This specification details the architecture for **100% dynamic, zero-hardcoding subagent routing** in Messa (`messa/agents/registry.py`). 

It ensures that any request involving a connected app or app connection flow unconditionally executes **Step 1 (Composio API)** first, and only falls back to **Step 2 (Browserbase / Deepsearch)** if the app action is unsupported.

---

## 1. Dynamic Per-Turn Connected Apps Injection (No Hardcoding)

Instead of static app lists in subagent descriptions, `messa/agents/registry.py` will dynamically fetch the user's active connected toolkits (via `_connected_toolkits_sync` or DB) on every turn and inject them into `integrations_agent`'s subagent `description` string:

### Dynamic Subagent Description Pattern:
```python
# Fetched per turn for current user (e.g. ['reddit', 'googlecalendar', 'slack'])
connected_slugs = get_user_connected_toolkits(user) 
connected_str = f" Currently connected for this user: [{', '.join(connected_slugs)}]." if connected_slugs else ""

integrations_agent_description = (
    f"Reaches Composio's 1,400+ app integrations (Todoist, Slack, Notion, GitHub, Instagram, Reddit, Google Calendar, and more) "
    f"that aren't native subagents.{connected_str} "
    "ALWAYS use this subagent FIRST whenever the user requests data, posts, or actions on these apps, or asks to connect ANY app."
)
```

---

## 2. Universal Connection & Fallback Routing Rules

### Rule A: Universal App Connection Rule
Any prompt asking to *"connect"*, *"link"*, *"authorize"*, or *"sync"* ANY app or 3rd-party service **MUST** delegate to `integrations_agent` immediately.

### Rule B: Strict 2-Step Flow Hierarchy
1. **Step 1 (Composio API):** Check `integrations_agent` FIRST for any app request.
2. **Step 2 (Browserbase Fallback):** Only if `search_integration_tools` confirms an app or action is unsupported by Composio, fall back to `deepsearch` browser automation.

### Rule C: Negative Boundary on `deepsearch`
In `messa/tools/deepsearch_tools.py`, update `deepsearch`'s subagent description to explicitly state:
> *"Do NOT use for 3rd-party apps (Reddit, Slack, Todoist, Notion, Google Calendar) that can be accessed via `integrations_agent` — use `integrations_agent` for those."*

---

## Developer Action Items

1. **`messa/agents/registry.py`:**
   - Dynamically build `integrations_agent`'s `description` field with the user's active connected toolkit slugs.
   - Add the Universal App Connection Rule to Messa's Orchestrator system prompt.
2. **`messa/tools/deepsearch_tools.py`:**
   - Add the negative boundary instruction to `deepsearch`'s `description` field.
