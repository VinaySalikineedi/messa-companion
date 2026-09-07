-- Migration 033: Active Task Scratchpad + persistent cross-agent, cross-
-- user Skills Playbook (docs/autonomous_integrations_and_task_memory_spec.md).
--
-- Built on a real production incident: Messa forgot her own pre-written
-- pitch draft mid-task (message-history truncation), and integrations_agent
-- made 15+ failed tool calls re-discovering that Google Sheets requires a
-- spreadsheet_id it already had from two turns earlier. Two independent
-- fixes, two tables.
--
-- Feature-flagged (config.SCRATCHPAD_AND_SKILLS_ENABLED) and shipped on its
-- own branch (feature/scratchpad-and-skills) precisely so it can be tested
-- hard before touching main/production -- see the tables' own comments for
-- the reliability/security reasoning behind each design choice.

-- ---------------------------------------------------------------------------
-- 1. active_tasks -- per-user, per-task working memory. `artifacts` holds
-- structured facts gathered mid-task (spreadsheet_id, pitch_draft,
-- recipients, sender_email, ...) so they survive message-history
-- truncation/compaction -- injected into the orchestrator's AND every
-- delegated subagent's system prompt while status is 'in_progress' or
-- 'waiting_user_input' (see registry.py's build_orchestrator and each
-- CompiledSubAgent's own _run).
--
-- `artifacts` is TEXT (JSON-serialized), matching this project's
-- established convention (db.py's own module docstring: "payload columns
-- are TEXT... not JSONB") rather than introducing JSONB as a second one
-- here. Partial-field updates still need to be atomic under concurrent
-- writers though (multiple subagents/sub-workers can touch the same task
-- in flight) -- db.update_active_task_artifacts gets that WITHOUT a
-- read-modify-write race by casting through ::jsonb for a single
-- statement's merge (COALESCE(artifacts,'{}')::jsonb || $new::jsonb) and
-- casting straight back to text for storage. One atomic UPDATE, relying on
-- Postgres's own per-row lock -- no application-level locking, no new
-- asyncpg jsonb codec, no change to what type the column actually is.
--
-- Only ONE open task per user is the intended model (db.start_active_task
-- reuses an existing open row rather than creating a second) -- this keeps
-- "which task's artifacts get injected" unambiguous.
CREATE TABLE IF NOT EXISTS active_tasks (
    task_id UUID PRIMARY KEY,
    user_id INTEGER NOT NULL,
    task_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',  -- in_progress | waiting_user_input | completed | failed
    artifacts TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS active_tasks_user_status_idx ON active_tasks (user_id, status);

-- Retention (explicit product decision, not "keep forever" or "delete
-- immediately"): a finished task keeps its row -- task_id/type/status/
-- timestamps, cheap and useful for support/debugging -- but its
-- `artifacts` payload (the actual PII: pitch drafts, recipient emails,
-- spreadsheet IDs) gets wiped after a short grace window
-- (config.ACTIVE_TASK_ARTIFACT_RETENTION_DAYS, default 7d), and the whole
-- row is dropped after a longer one (config.ACTIVE_TASK_ROW_RETENTION_DAYS,
-- default 60d). Enforced by db.purge_stale_active_tasks, run daily from
-- server.py's _production_scratchpad_cleanup_loop -- same shape as the
-- existing memory-batch daily job (migration 027).

-- ---------------------------------------------------------------------------
-- 2. agent_skills -- the persistent, GLOBAL (cross-user, BY DESIGN -- see
-- the spec doc's security/reliability discussion) playbook of learned
-- tool/site fixes: "Google Sheets needs spreadsheet_id, not a title",
-- "amazon.com's search box is at selector X", etc. Deliberately NOT
-- scoped to one user -- a procedural lesson about how a tool or website
-- behaves isn't personal data, and sharing it is the entire point (every
-- user's agents get smarter from every other user's discoveries).
--
-- UNIQUE(agent_type, domain, problem_pattern) is the WHOLE dedup
-- mechanism: a second agent (or the same one later) rediscovering the
-- same fix does INSERT ... ON CONFLICT DO UPDATE (db.upsert_skill), which
-- atomically bumps success_count/last_used_at on the SAME row instead of
-- creating a duplicate -- no read-then-write race, no app-level locking,
-- same atomicity reasoning as active_tasks.artifacts above. This is also
-- the mechanism that keeps growth bounded per-domain rather than
-- unbounded: popular domains accumulate confidence (success_count), not
-- row count.
--
-- source_user_id/source_task_id are for audit/traceability ONLY (spec
-- doc's security section: if a bad or adversarial skill slips past the
-- content screening in tools/scratchpad_tools.py, this is how its blast
-- radius gets traced and it gets purged) -- never used for access
-- control; every user's agents can read every row here, that IS the
-- feature.
CREATE TABLE IF NOT EXISTS agent_skills (
    skill_id UUID PRIMARY KEY,
    agent_type TEXT NOT NULL,
    domain TEXT NOT NULL,
    problem_pattern TEXT NOT NULL,
    solution_recipe TEXT NOT NULL,
    success_count INTEGER NOT NULL DEFAULT 1,
    source_user_id INTEGER,
    source_task_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (agent_type, domain, problem_pattern)
);

-- The read-time half of "how does this stay fast as skills grow
-- exponentially" (explicit product question this was designed against):
-- every lookup (db.search_skills) filters on the exact (agent_type,
-- domain) pair first -- this index keeps that a narrow, indexed lookup
-- covering just one toolkit/site's rows, regardless of how many other
-- domains or agent types exist in the table.
CREATE INDEX IF NOT EXISTS agent_skills_lookup_idx ON agent_skills (agent_type, domain, success_count DESC, last_used_at DESC);

-- Write-time bound (config.SKILLS_MAX_PER_DOMAIN, default 50): enforced in
-- db._evict_excess_skills, called inline from every upsert_skill write --
-- not a separate cron job, so the cap holds even if a cleanup job is ever
-- late or fails to run.
