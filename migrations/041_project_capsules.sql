-- V3-autonomous.md Phase 6 ("Autonomous Project Capsules & Multi-Modal Vault").
--
-- Deliberately named `project_capsules` -- NOT `user_projects`, the doc's
-- literal section-5 proposal -- to avoid any collision with the
-- pre-existing, UNRELATED `projects` table (migrations/002_projects_and_
-- channel.sql: simple task-grouping, id/title/status, FK'd from
-- tasks.project_id, backing db.list_projects/get_or_create_project). A
-- project capsule is a different concept entirely (a delegated,
-- autonomous, multi-week objective with its own vault and timeline), so
-- every new table/column here uses a distinct, unambiguous vocabulary
-- rather than reusing or overloading the old `projects` naming.
--
-- Deliberately a THIN WRAPPER around the existing autonomous-routine
-- engine (cron_jobs / routines_agent, migrations/024_task_routines.sql)
-- rather than a parallel scheduler: cron_job_id below links a capsule to
-- the ONE cron_jobs row that actually drives its cadence checks, so
-- creation, pause/resume/cancel, retry/backoff, expiry, and conflict-
-- auto-supersede are all the SAME already-battle-tested mechanism an
-- ordinary autonomous routine uses -- not reimplemented here. See
-- db._insert_project_capsule and db.set_cron_job_status's own docstrings
-- for exactly how a capsule's status stays in sync with its cron job.
--
-- Follows this project's established additive/no-op-safe convention:
-- db.py guards every read/write with _has_table, so a deployment that
-- hasn't run this migration yet just sees the whole feature as a no-op
-- (config.PROJECT_CAPSULES_ENABLED is the separate, explicit kill switch
-- for once it HAS run).
CREATE TABLE IF NOT EXISTS project_capsules (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cron_job_id INT REFERENCES cron_jobs(id) ON DELETE SET NULL,
    title VARCHAR(255) NOT NULL,
    goal TEXT NOT NULL,
    -- active | paused | completed | cancelled | expired -- kept up
    -- automatically in sync with cron_job_id's own cron_jobs.status by
    -- db.set_cron_job_status (see that function's docstring), never
    -- written directly by any other caller.
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    outcome_summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_project_capsules_user ON project_capsules(user_id);
CREATE INDEX IF NOT EXISTS ix_project_capsules_cron_job ON project_capsules(cron_job_id);

-- project_vault_assets: the "Multi-Modal Vault" -- user-supplied
-- documents/photos/receipts filed against a capsule at Messa's own
-- judgment (routines_tools.py's add_project_capsule_asset), never filed
-- automatically. source_url is the inbound Sendblue CDN link, kept
-- AS-IS -- deliberately NOT downloaded and re-hosted onto Messa's own
-- durable file storage in this phase (unlike an OUTBOUND share, which
-- already gets a durable config.LIVE_VIEW_BASE_URL/files/{token} link --
-- see tools/document_tools.py). That's a real, disclosed limitation:
-- Sendblue's own CDN retention window applies to whatever's filed here.
-- Re-hosting inbound attachments the same durable way is a reasonable
-- follow-up, not required for this phase's core "walk away and let Messa
-- manage it" capability. Also deliberately just source_url + description
-- (TEXT), not the doc's literal file_url/metadata JSONB -- no consumer
-- today needs structured metadata beyond a link and a caption.
CREATE TABLE IF NOT EXISTS project_vault_assets (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES project_capsules(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_project_vault_assets_project ON project_vault_assets(project_id);

-- project_timeline_events: a plain-language audit trail a user (or a
-- future dashboard) can read back -- "emailed Delta asking for a status
-- update", "received the $850 refund confirmation". Deliberately a
-- single event_text column, not the doc's literal structured JSONB
-- event_payload -- every reader today (get_project_capsule_details) only
-- ever needs to show a human a line of text, and a structured payload
-- with no defined schema or reader yet is exactly the kind of premature
-- structure this phase's "don't bloat" mandate says to skip until
-- something actually needs to query it.
CREATE TABLE IF NOT EXISTS project_timeline_events (
    id SERIAL PRIMARY KEY,
    project_id INT NOT NULL REFERENCES project_capsules(id) ON DELETE CASCADE,
    event_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_project_timeline_events_project ON project_timeline_events(project_id);
