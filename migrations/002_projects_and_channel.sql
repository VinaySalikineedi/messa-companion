-- Additive migration for Phase 1 of the Messa multi-agent harness.
-- Nothing here modifies or drops existing data; every statement is
-- IF NOT EXISTS / safe to re-run. Run this once against your Neon DB
-- (psql, or paste into the Neon SQL editor) before using messa/db.py's
-- project-tracking helpers. Everything else in the app works even if
-- you skip this -- db.py checks for these before using them.

-- Groups related requests/tasks together so Messa can track "your website
-- redesign" as one thread across multiple messages, per the original ask
-- ("keeps track of user's requests as a project"). Not present in the
-- original neon-schema.sql.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'project_status') THEN
        CREATE TYPE project_status AS ENUM ('active', 'completed', 'archived');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS projects (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    status project_status NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_projects_user_id ON projects(user_id);
CREATE INDEX IF NOT EXISTS ix_projects_status ON projects(status);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS project_id INT REFERENCES projects(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS ix_tasks_project_id ON tasks(project_id);

-- Lets message_history record which channel a message came from
-- (cli / sms / imessage / email) once Phase 2 adds more channels than the CLI.
ALTER TABLE message_history ADD COLUMN IF NOT EXISTS channel VARCHAR(20) NOT NULL DEFAULT 'cli';
