-- Migration 024: task routines (Messa-does-it-herself automations,
-- one-shot scheduling, deadlines, retries, digests, escalation).
--
-- Deliberately additive and defensive, matching every migration before it
-- in this file: `IF NOT EXISTS` everywhere, no column made NOT NULL without
-- a DEFAULT (so existing rows never need a backfill pass), and no ALTER
-- TYPE on the cron_job_status enum (a self-terminating/expired job just
-- reuses the existing 'cancelled' status -- the finer-grained "why" lives
-- in the new `meta` JSONB column as `ended_reason`, not a new enum value).
--
-- execution_mode distinguishes "remind the USER to do X themselves"
-- ('notify') from "MESSA does X herself and reports back" ('autonomous').
-- Every pre-existing row defaults to 'autonomous' -- that's a no-op change
-- in practice, since every cron job today already re-invokes Messa's full
-- agent turn (server.py's _production_cron_loop), which IS autonomous
-- behavior; the 'notify' path is the new, cheaper, deterministic one.
--
-- One-shot scheduling needs no new column at all: `next_run_at` already
-- means "when to fire next" for both cases -- a one-shot job just sets
-- cron_expression to the literal sentinel 'once' (never fed to croniter)
-- and, after firing, gets marked terminal instead of rescheduled. See
-- messa/db.py's insert_routine/_insert_cron_job for where this is written.
--
-- `meta` is TEXT (JSON-serialized), not JSONB -- matching this project's
-- one existing convention for structured columns (see pending_actions/
-- audit_logs' own `payload` columns and migrations/004's docstring on
-- deliberately not introducing JSONB as a second one): messa/db.py hand
-- json.dumps/json.loads's it at the boundary, same as everywhere else.
ALTER TABLE cron_jobs ADD COLUMN IF NOT EXISTS execution_mode VARCHAR(20) NOT NULL DEFAULT 'autonomous';
ALTER TABLE cron_jobs ADD COLUMN IF NOT EXISTS meta TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS ix_cron_jobs_execution_mode ON cron_jobs(execution_mode);

-- "While you were away" digest queue (sub-feature #3): an autonomous or
-- notify job tagged digest=true in its `meta` writes its result here
-- instead of texting immediately; messa/server.py's new
-- _production_digest_loop flushes each user's queued items into one
-- message at their local morning hour (or once a small backstop count is
-- reached, so a quiet user isn't left waiting a full day for one result).
CREATE TABLE IF NOT EXISTS digest_queue (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cron_job_id INT REFERENCES cron_jobs(id) ON DELETE SET NULL,
    message_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_digest_queue_user_id ON digest_queue(user_id);
