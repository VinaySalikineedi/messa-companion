# Observations of System Behavior & Failure Modes (dumb-messa.md)

This document lists factual observations from system logs, database records, and codebase tracing regarding Messa's behavior during live sessions on September 12, 2026.

---

## 1. Context Eviction & Short-Term Memory Loss
* **Code Location:** `messa/cli.py:764` (`get_recent_messages(user.user_id, limit=12)`)
* **Observations:**
  * The database query retrieves a hard-coded maximum of 12 message rows from the `message_history` table.
  * Because long assistant replies are split into multiple SMS chunks (governed by `_SPLIT_TARGET_CHARS = 200` in `cli.py`), a single assistant turn typically consumes between 3 and 8 message rows.
  * 12 message rows represents approximately 1.5 to 2 conversational turns of history.
  * By Turn 3 of a conversation, prior actions taken in Turn 1 are evicted from context.
  * In Turn 7 (16:59 UTC), Messa stated in chat:
    > *"When the agent sent them, it found each thread already had a copy from your Messa address sent about eight minutes earlier... That contradicts my earlier note that wave 1 went from Gmail. I can't verify which was true, so I won't guess."*
  * Messa could not verify whether she had sent emails from Gmail or Messa email 8 minutes earlier because the earlier turn had been evicted from the 12-message history window.

---

## 2. The Ghost Approval Nudge Loop
* **Code Location:** `messa/cli.py:889-914` (`ONE_TOUCH_APPROVAL_NUDGE_ENABLED`)
* **Observations:**
  * When incoming user text matches `reliability.is_bare_affirmation(text)` (e.g., `"ok"`, `"yes"`, `"great, thanks!"`, `"all good"`), the system checks `db.list_pending_actions(user.user_id)`.
  * The query `list_pending_actions` contains no timestamp filter, expiration check, or time-to-live (TTL).
  * As of September 12, 2026, the database contained 12 unresolved pending actions for user 1 dating back to August 27, 2026 (including `#20 create_reminder: "Call Mom"` and `#21 create_reminder: "Buy iPhone wireless charger"`).
  * When the user sent closing or polite remarks like `"Ok"` (Turn 6) or `"Great, thanks!"` (Turn 7), the server evaluated `pending` as non-empty.
  * The server logged:
    > `[system] Messa: this reply reads as a clear approval but nothing was confirmed or rejected this turn, and an action is still pending -- retrying once with a nudge.`
  * The server automatically re-invoked `run_turn` with an injected synthetic prompt:
    > `(auto-check, not from the user: their last message reads as a clear approval, and there's at least one action still awaiting confirmation -- check list_pending_actions. If it's for that, call confirm_pending_action now. If their message was about something else, ignore this.)`

---

## 3. Duplicate Email Dispatch
* **Code Location:** Triggered during the automated approval nudge retry in `messa/cli.py:911`
* **Observations:**
  * When the synthetic nudge was injected following `"Great, thanks!"`, the orchestrator re-evaluated the task state.
  * The agent checked `list_pending_actions`, found items `#20` and `#21`, rejected unrelated items `#76` and `#77`, and questioned whether the previous email dispatch to Cross River and Column had completed.
  * The agent delegated to `personal_inbox_agent` to send the emails again.
  * Duplicate emails were dispatched from `vinay@textmessa.com` to `info@crossriver.com` and `hi@column.com` at 16:57 UTC, approximately 8 minutes after identical emails had been dispatched at 16:49 UTC.

---

## 4. Message Flooding & Real-Time Monologue Streaming
* **Code Location:** `messa/cli.py:420` (`_on_ai_message`), `messa/cli.py:79` (`_SPLIT_TARGET_CHARS = 200`)
* **Observations:**
  * In `cli.py`, the callback `on_ai_message` immediately invokes `sendblue.send_message()` for every intermediate text chunk produced during an agent run.
  * In Turn 7 alone (prompted by `"Great, thanks!"`), the system generated and sent **21 separate SMS messages** to the user between 16:55:54 UTC and 16:59:39 UTC.
  * Messages streamed to the user's phone included intermediate reasoning, internal status checks, and self-corrections, such as:
    * *"Actually sending it now — one sec."*
    * *"I checked the pending list. Nothing matches the 'Ok'..."*
    * *"The open items are older, unrelated reminders, calendar events, and inbox-watcher routines. None were approved by that message, so I’m not confirming any of them and took no action."*
    * *"Resending both from your Messa address now."*

---

## 5. Third-Person Mandate & Gmail Outreach Insistence
* **Code Location:** `messa/agents/registry.py:1177-1179`
* **Observations:**
  * The system prompt contained the following instruction:
    > *"Whenever you draft or send from their Messa address, always write third-person on their behalf ('<Name> asked me to confirm...') -- never first-person as the user, never sign off with their name."*
  * In Turn 1 (16:35 UTC) and during morning sessions, when requested to conduct founder partnership outreach, Messa reasoned that founder outreach required speaking as the founder (first-person), which the prompt prohibited on the Messa email address.
  * Messa asserted in chat:
    > *"Plan is I send from your Gmail as you — founder outreach should come from Vinay."*
  * Messa repeatedly sought confirmation to send from Gmail and noted that Messa-addressed emails were sent *"third-person on your behalf"*.

---

## 6. Complete Inbound Reaction (Tapback) Failure
* **Code Location:** `messa/server.py:1217-1221`, `messa/config.py:660` (`REACTION_CLASSIFIER_TIMEOUT_SECONDS = 6.0`)
* **Observations:**
  * `_react_to_inbound` runs `_pick_contextual_reaction` in a background task wrapped in `asyncio.wait_for(..., timeout=6.0)`.
  * In live runtime container logs on Hugging Face Spaces across all afternoon turns, every inbound message logged:
    > `[system] [inbound reaction] classifier call failed, skipping:`
  * In Python, `str(asyncio.TimeoutError()) == ""` (an empty string), matching the blank output after `skipping:`.
  * OpenRouter response latency for the classifier model (`~deepseek/deepseek-v4-flash-latest`) ranged from 9.03 seconds to 31.33 seconds during the session.
  * Because model latency exceeded the 6.0-second cutoff on every call, 100% of inbound reaction attempts timed out and were dropped. Zero tapback reactions were delivered to the user during the session.

---

## 7. Competing Project Architectures
* **Code Location:** `messa/agents/registry.py:159-175` (`track_project`, `list_active_projects`) vs. `messa/tools/routines_tools.py` (`project_capsules`)
* **Observations:**
  * Two distinct project systems existed simultaneously in the codebase:
    1. Legacy `track_project` / `list_active_projects` directly on the orchestrator tool list (Migration 002), which only recorded an ID and title string without milestone tracking or file attachments.
    2. Phase 6 `project_capsules` inside `routines_agent` (Migration 041), which maintains timelines, document vaults, and autonomous check-in cron jobs.
  * The orchestrator prompt instructed Messa to use `track_project`, causing project requests to use the legacy stub rather than the persistent capsule system until the prompt was updated to direct projects to `routines_agent`.
