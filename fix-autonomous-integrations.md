# Production Readiness Review: Active Task Scratchpad & Skills Playbook
**Branch:** `feature/scratchpad-and-skills`  
**Target:** `main`  
**Recommendation:** **DO NOT MERGE / DO NOT DEPLOY YET**  
**Priority:** P0 (Blocks Release)

---

## Executive Summary

The v1 implementation of the **Active Task Scratchpad** and **Skills Playbook** (`migrations/033_scratchpad_and_skills.sql`, `messa/tools/scratchpad_tools.py`, wiring across declarative and compiled subagents) is a strong foundation:
- Schema design and indexes are clean.
- Deduplication and security screening (`_screen_skill_text`) effectively block prompt injections, credentials, and URLs.
- The kill switch and graceful pre-migration degrade function as expected.

However, **two critical bugs** and **one major architectural gap** must be addressed before this branch is safe for production. Without these fixes, the system will delete high-performing skills, leak stale task artifacts across user conversations indefinitely, and remain vulnerable to context-window overflow.

---

## Critical Blockers (Must Fix Before Merge)

### 1. [CRITICAL BUG] Inverted Eviction SQL Deletes the Best Skills
* **File:** [`messa/db.py:1242-1249`](file:///Users/robocafedesktop/Documents/textMessa/messa/db.py#L1242-L1249) in `_evict_excess_skills`
* **Code:**
  ```sql
  DELETE FROM agent_skills WHERE skill_id IN (
      SELECT skill_id FROM agent_skills
      WHERE agent_type = $1 AND domain = $2
      ORDER BY success_count ASC, last_used_at ASC
      OFFSET $3
  )
  ```
* **The Defect:**
  When a domain exceeds `SKILL_DOMAIN_CAP` (50), sorting by `success_count ASC` and taking `OFFSET 50` skips the 50 lowest-success skills and selects everything *above* them.
  **This deletes the most frequently used, highest-success skills while permanently keeping broken or single-use skills.**
* **Why the Unit Tests Missed It:**
  [`tests/test_scratchpad_and_skills.py:339`](file:///Users/robocafedesktop/Documents/textMessa/tests/test_scratchpad_and_skills.py#L339) only asserted that `"DELETE FROM agent_skills"` was present in the generated SQL string using a mock; it never asserted sort direction or tested with actual row data.
* **Fix Required:**
  Sort in descending order so `OFFSET 50` selects the lowest-performing excess skills for deletion:
  ```sql
  DELETE FROM agent_skills WHERE skill_id IN (
      SELECT skill_id FROM agent_skills
      WHERE agent_type = $1 AND domain = $2
      ORDER BY success_count DESC, last_used_at DESC
      OFFSET $3
  )
  ```

---

### 2. [ARCHITECTURAL GAP] Active Tasks Are Never Completed (Permanent Context Bleed & DB Bloat)
* **Files:** [`messa/db.py:1172`](file:///Users/robocafedesktop/Documents/textMessa/messa/db.py#L1172), [`messa/tools/scratchpad_tools.py`](file:///Users/robocafedesktop/Documents/textMessa/messa/tools/scratchpad_tools.py), [`messa/server.py`](file:///Users/robocafedesktop/Documents/textMessa/messa/server.py)
* **The Defect:**
  1. `set_active_task_status(task_id, status)` is implemented in `db.py`, but **is never called anywhere in application code**, nor is there an agent tool (e.g. `complete_task`) exposed to agents to mark a task finished.
  2. The only status update implemented in `update_task_scratchpad` transitions tasks to `'in_progress'` or updates artifacts.
* **Production Impact:**
  - **Cross-Task Amnesia / Pollution:** A user's active task stays `'in_progress'` forever. Days or weeks later, any subsequent request from that user will continue loading the old task's scratchpad (`pitch_draft`, `spreadsheet_id`, etc.) into Messa's prompt.
  - **Broken Retention Policy:** The daily cleanup job `purge_stale_active_tasks` in `server.py` runs:
    ```sql
    DELETE FROM active_tasks WHERE status IN ('completed', 'failed') AND updated_at < ...
    ```
    Because tasks never leave `'in_progress'`, **active task rows and artifacts are never purged**, causing unbounded database growth.
* **Fix Required:**
  1. Add a `complete_active_task(task_id: str, summary: str = "")` tool in `scratchpad_tools.py` so the agent can mark tasks as `'completed'`.
  2. Alternatively/additionally, update `purge_stale_active_tasks` to also expire abandoned `'in_progress'` tasks whose `updated_at` is older than 7 days (or mark them as `'abandoned'`/`'completed'`).

---

### 3. [HIGH SEVERITY] Unbounded Artifact Size (Context Window Overflow Risk)
* **File:** [`messa/tools/scratchpad_tools.py:133-149`](file:///Users/robocafedesktop/Documents/textMessa/messa/tools/scratchpad_tools.py#L133-L149)
* **The Defect:**
  `update_task_scratchpad(fields)` accepts arbitrary JSON and merges it directly into `active_tasks.artifacts` without size validation or depth limiting.
* **Production Impact:**
  If an agent stores scraped HTML, raw email bodies, or massive API payloads (e.g., 50–100KB) into `artifacts`, that entire payload is injected into every subagent prompt on every turn, triggering the Gemini `400 InvalidArgument / Context Window Exceeded` error experienced in today's incident.
* **Fix Required:**
  Enforce guardrails in `update_task_scratchpad`:
  - Cap individual field values (e.g., max 2,000 characters per string).
  - Cap total artifacts payload size (e.g., max 10,000 characters serialized).
  - Return a clean validation error if the agent attempts to dump oversized text.

---

## Gaps from Morning Incident Still Unresolved in v1

The original incident this morning had four core failure modes:

| Morning Incident Failure | Handled in `feature/scratchpad-and-skills`? | Status / Recommendation |
| :--- | :---: | :--- |
| **Lost Pitch Draft & Spreadsheet ID** | **Partially** | Solved by `active_tasks.artifacts` during the active task, but will leak across tasks until Task Completion (#2 above) is implemented. |
| **15+ Repeated Google Sheets API Failures** | **Partially** | `save_skill` / `search_skills` allows saving discovered fixes. However: (1) Inverted eviction (#1) deletes them; (2) `integrations_agent` does not have web search or dynamic tool search to find the fix itself; (3) Agent must manually query skills. |
| **Google Drive Disconnected (No User Prompt)** | **No** | Proactive 1-click connect URL generation on `NOT CONNECTED` errors is not yet implemented in `integrations_agent`. |
| **Context Window 400 Errors** | **No** | The 12-message history window in `messa/cli.py` is still naive message slicing without turn-based compaction, and scratchpad injection increases token pressure. |
| **Plumbing Error Leaks Over Text** | **No** | No sanitization was added to prevent raw backend/Composio error dictionaries from leaking directly into SMS responses. |

---

## Action Plan for the Team

### Step 1: Fix `messa/db.py`
```python
# In messa/db.py: _evict_excess_skills
# Change ORDER BY from ASC to DESC:
DELETE FROM agent_skills WHERE skill_id IN (
    SELECT skill_id FROM agent_skills
    WHERE agent_type = $1 AND domain = $2
    ORDER BY success_count DESC, last_used_at DESC
    OFFSET $3
)
```

### Step 2: Implement Task Completion Lifecycle
1. Add `complete_task` tool to `messa/tools/scratchpad_tools.py`:
   ```python
   async def complete_task(summary: str) -> str:
       """Mark the user's current active task as completed once all user requests are fulfilled."""
       ...
   ```
2. Update `purge_stale_active_tasks` in `messa/db.py` to also prune abandoned tasks:
   ```sql
   UPDATE active_tasks SET status = 'abandoned'
   WHERE status = 'in_progress' AND updated_at < NOW() - INTERVAL '7 days';
   ```

### Step 3: Add Size Guardrails in `update_task_scratchpad`
Validate that any dictionary passed into `update_task_scratchpad` does not exceed 10KB total, and individual string fields do not exceed 2,000 characters.

### Step 4: Add Real-Data Test Coverage
Add tests in `tests/test_scratchpad_and_skills.py` that insert 55 simulated rows into `agent_skills` and verify that the 5 lowest-performing skills are evicted, confirming the top 50 remain untouched.

---

## Deployment Readiness Criteria

Do not merge `feature/scratchpad-and-skills` to `main` or deploy to Hugging Face Spaces until:
1. The eviction SQL query order is flipped to `DESC`.
2. Active tasks have a completion path and stale `in_progress` cleanup.
3. Artifact input is bounded to prevent context overflows.
4. Unit tests pass against live simulated row eviction.
