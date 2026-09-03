-- Migration 026: admin broadcast messages (preview -> approval -> execute).
--
-- Purely additive: a new table plus one new gated action_type
-- ("broadcast_message", handled in Python since action_type is VARCHAR(100)
-- not an ENUM -- see db.py's own comment on migration 009 for why that
-- earlier change makes this need no migration of its own). Nothing here
-- changes any existing behavior -- only an account with users.is_admin=true
-- (migrations/023_usage_limits.sql) ever sees the tool that creates a row
-- in this table.
CREATE TABLE IF NOT EXISTS broadcasts (
    id SERIAL PRIMARY KEY,
    created_by INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_text TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending -> sending -> completed
    total_recipients INT,
    sent_count INT NOT NULL DEFAULT 0,
    failed_count INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_broadcasts_status ON broadcasts(status);
