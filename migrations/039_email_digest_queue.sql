-- Migration 039: Email digest queue (V3-autonomous.md Phase 3, "Three-Tier
-- Inbound Email Triage"), built on top of migration 038's user_lists.
--
-- Holds triaged (non-VIP) inbound personal-email one-line summaries between
-- arrival and the briefing they get rolled into -- 'daily' items flush into
-- the next morning_briefing (config.DEFAULT_BRIEFINGS), 'weekly' items
-- flush into the next Sunday evening_briefing (see messa/email_triage.py
-- and messa/briefings.py).
--
-- Deliberately a SEPARATE table from digest_queue (migrations/
-- 024_task_routines.sql): that one holds routine notify/autonomous "while
-- you were away" results and is flushed by an entirely different poller
-- (server.py's _production_digest_loop, on a local-hour/backstop-count
-- cadence) than this one (briefings.py's morning/Sunday-evening renders).
-- Sharing one table would risk an email item getting swept up and sent
-- early as a "while you were away" text instead of appearing inside the
-- briefing it was actually meant for.
CREATE TABLE IF NOT EXISTS email_digest_queue (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tier VARCHAR(10) NOT NULL CHECK (tier IN ('daily', 'weekly')),
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The one query shape this exists to serve fast -- db.get_pending_email_
-- digest_items runs on every due morning/evening briefing render.
CREATE INDEX IF NOT EXISTS ix_email_digest_queue_lookup ON email_digest_queue (user_id, tier, created_at);
