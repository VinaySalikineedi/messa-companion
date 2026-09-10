# Phase 6 (Project Capsules) — QA Stress Test & Verification Report

**Date**: September 10, 2026  
**Audited Branch**: `feature/v3-phase6-project-capsules`  
**Latest Commit**: `e429c58` (`fix(v3-phase6): production-harden project capsules per QA report`)  
**Base Commit**: `af1024d` (Phase 5 deployed on `main`)  
**Status**: **PRODUCTION READY** 🚀  

---

## 1. Executive Summary

Following the initial adversarial audit, Claude delivered a centralized architectural hardening commit (`e429c58`). We re-tested the entire feature across all operational failure vectors, stress-tested real-life cadence loops, and ran the full 40-module regression suite.

- **Full Release Regression Suite**: **40 Passed, 0 Failed** (100% Green).
- **Phase 6 Dedicated Suite**: **88 Passed, 0 Failed** (`tests/test_v3_phase6_project_capsules.py`).
- **Verdict**: **PRODUCTION READY**. All 6 architectural gaps and crash risks have been completely eliminated.

---

## 2. Verification of the 6 Remediated Issues

### Issue 1: Pre-Migration Graceful Degradation
* **Prior Flaw**: `finish_project_capsule`, `add_project_capsule_asset`, `list_project_capsule_assets`, `log_project_capsule_event`, and `list_project_capsule_events` executed queries without checking `_has_table`, crashing with `UndefinedTableError` if migration 041 had not run.
* **Fix Verified**: Every single project read/write function in `messa/db.py` now checks `_has_table` and returns safe empty defaults (`{}` or `[]`). Pre-migration zero-error degradation verified across all 6 functions.
* **Status**: **RESOLVED & VERIFIED** ✅

---

### Issue 2: Conflict Auto-Supersede State Desynchronization
* **Prior Flaw**: `_auto_supersede_conflicting_routine` cancelled routines via raw SQL, leaving linked `project_capsules` frozen in `status = 'active'`. Dead projects continued being advertised in the orchestrator system prompt.
* **Fix Verified**: `_auto_supersede_conflicting_routine` now updates the superseded job's meta with `ended_reason = 'superseded'` and calls `_sync_linked_project_capsule_status`.
  - Linked capsule status is atomically updated to `'cancelled'`.
  - A timeline audit event is logged: `"Cancelled -- superseded by a newer routine covering the same recipient."`
  - Dead capsules are immediately excluded from `_active_project_capsules_paragraph`.
* **Status**: **RESOLVED & VERIFIED** ✅

---

### Issue 3: Cadence Loop Disconnect & Outcome Erasure
* **Prior Flaw**: The background cadence loop (`_production_cron_loop`) did not inform the agent of the `project_id`, instructed the agent to call the generic `finish_routine`, and erased the outcome summary and completion timeline event on completion.
* **Fix Verified**:
  1. `server._fire_autonomous_routine` checks `db.get_project_capsule_by_cron_job(job["id"])`. When an active project capsule is found, the system prompt injected into the turn explicitly provides:
     - Project Capsule ID, Title, and Goal.
     - Direct instructions to inspect context via `get_project_capsule_details(project_id=...)`.
     - Direct instructions to resolve via `finish_project_capsule(project_id=...)` or log progress via `log_project_capsule_event`.
  2. If the model calls the generic `finish_routine(cron_id=...)`, `_sync_linked_project_capsule_status` automatically copies `outcome` onto `project_capsules.outcome_summary` and logs the completion timeline entry.
* **Status**: **RESOLVED & VERIFIED** ✅

---

### Issue 4: Confirmation Crash on Long Titles (`VARCHAR(255)` Overflow)
* **Prior Flaw**: Titles exceeding 255 characters were accepted at proposal time and crashed with a PostgreSQL truncation exception on confirmation.
* **Fix Verified**: `propose_create_project_capsule` now validates `len(title) <= config.PROJECT_CAPSULE_TITLE_MAX_LENGTH` (200 characters). Titles exceeding 200 characters are intercepted immediately with a clean, actionable error before reaching the database. Titles up to 200 characters confirm cleanly.
* **Status**: **RESOLVED & VERIFIED** ✅

---

### Issue 5: Silent Attachment Drop on Media Understanding Failures
* **Prior Flaw**: `[attachment_url: ...]` was nested inside `if media_note:`. Any attachment where vision understanding timed out, errored, or encountered an unreadable format (e.g. `.docx`, `.zip`) lost its URL tag, preventing vault filing.
* **Fix Verified**: In `server._process_inbound`, `[attachment_url: {media_url}]` is now appended unconditionally whenever `media_url` is present and `config.PROJECT_CAPSULES_ENABLED` is true. If media understanding yields `None`, a fallback placeholder note is provided so the LLM retains full context and the raw URL.
* **Status**: **RESOLVED & VERIFIED** ✅

---

### Issue 6: Orphaned Project Marked "Completed" on Cancel
* **Prior Flaw**: If a project capsule's underlying cron job was deleted (`cron_job_id` became `NULL`), calling `cancel_project_capsule` called `finish_project_capsule`, erroneously marking the abandoned project as `'completed'`.
* **Fix Verified**: Created dedicated `db.cancel_orphaned_project_capsule`. It directly updates `project_capsules SET status = 'cancelled'` and logs `"Cancelled by the user."` to the timeline.
* **Status**: **RESOLVED & VERIFIED** ✅

---

## 3. Regression Suite Results (40/40 Passing)

Ran `bash tests/run_release_regression.sh` across all 40 test modules:

```text
==================================================================
SUMMARY: 40 passed, 0 failed
  [PASS] tests/test_privacy_page.py
  [PASS] tests/test_onboarding_and_email_db.py
  [PASS] tests/test_onboarding_and_deepsearch_msg.py
  [PASS] tests/test_onboarding_email_provisioning.py
  [PASS] tests/test_persona_prompt_and_guardrail.py
  [PASS] tests/test_assistant_email_persona.py
  [PASS] tests/test_email_persona_rigorous.py
  [PASS] tests/test_app_preferences.py
  [PASS] tests/test_routing_boundaries.py
  [PASS] tests/test_disconnect_switch.py
  [PASS] tests/test_default_email_provider.py
  [PASS] tests/test_app_connect_queue.py
  [PASS] tests/test_message_splitting.py
  [PASS] tests/test_inbound_reactions.py
  [PASS] tests/test_progressive_tapbacks.py
  [PASS] tests/test_turn_control.py
  [PASS] tests/test_pure_acknowledgment.py
  [PASS] tests/test_double_texting.py
  [PASS] tests/test_region_gate.py
  [PASS] tests/test_minimal_questions_prompt.py
  [PASS] tests/test_contact_sharing.py
  [PASS] tests/test_fth_upgrades.py
  [PASS] tests/test_fth_consent_and_form_js_live.py
  [PASS] tests/test_scratchpad_and_skills.py
  [PASS] tests/test_call_plans_usage.py
  [PASS] tests/test_vapi_channel.py
  [PASS] tests/test_call_control_and_activity.py
  [PASS] tests/test_call_tools_security.py
  [PASS] tests/test_call_tools_flow.py
  [PASS] tests/test_call_webhook.py
  [PASS] tests/test_media_understanding.py
  [PASS] tests/test_reliability_hardening.py
  [PASS] tests/test_subagent_freshness.py
  [PASS] tests/test_workspace_asset_registry.py
  [PASS] tests/test_v3_phase1_approvals_and_supersede.py
  [PASS] tests/test_v3_phase2_user_lists.py
  [PASS] tests/test_v3_phase3_email_triage.py
  [PASS] tests/test_v3_phase4_latency.py
  [PASS] tests/test_v3_phase5_meeting_dossiers.py
  [PASS] tests/test_v3_phase6_project_capsules.py
==================================================================
```

---

## 4. Final Deployment Checklist for Phase 6

1. **Apply Migration 041**:
   Run `migrations/041_project_capsules.sql` against the live Neon database.
2. **Merge & Push**:
   Merge `feature/v3-phase6-project-capsules` into `main` and push to GitHub (`origin main`) and Hugging Face (`hf main:main`).
3. **Verify Health**:
   Confirm HTTP 200 on `https://live.textmessa.com/health`.
