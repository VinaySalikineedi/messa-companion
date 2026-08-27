-- Additive migration: gives deepsearch (formerly "browser_agent") somewhere
-- to persist progress, so a run that gets cut off by its step limit can be
-- resumed instead of starting over. Requested after testing showed Messa
-- correctly auto-retrying a stalled deepsearch task, but with the subagent
-- starting completely fresh each time and re-doing already-finished work.
--
-- messages/summary follow the same "JSON as TEXT" convention as
-- pending_actions.payload / audit_logs.payload in your original schema,
-- rather than introducing JSONB as a second convention.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'deepsearch_status') THEN
        CREATE TYPE deepsearch_status AS ENUM ('active', 'completed', 'abandoned');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS deepsearch_sessions (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    status deepsearch_status NOT NULL DEFAULT 'active',
    messages TEXT NOT NULL DEFAULT '[]',  -- JSON-serialized LangChain message list (full resumable transcript)
    summary TEXT,                          -- latest progress/findings summary, for quick display
    steps_used INT NOT NULL DEFAULT 0,     -- cumulative loop iterations across all resumes, for visibility
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_deepsearch_sessions_user_id ON deepsearch_sessions(user_id);
CREATE INDEX IF NOT EXISTS ix_deepsearch_sessions_status ON deepsearch_sessions(status);
CREATE INDEX IF NOT EXISTS ix_deepsearch_sessions_updated_at ON deepsearch_sessions(updated_at);
