-- Additive migration: durable state for connecting a user's Gmail account
-- via Composio OAuth, and reliably telling them once it's live.
--
-- Two different code paths write/read these, deliberately decoupled, same
-- shape as migrations/008_deepsearch_human_help.sql: request_email_connection
-- (messa/tools/email_tools.py) creates a row the moment it generates a
-- Composio connect link, so the follow-up doesn't depend on that same
-- in-flight agent call still being alive; server.py's
-- _production_email_connection_poll_loop is a separate background task that
-- polls Composio for that row's real connection status and sends exactly
-- one confirmation text once it goes ACTIVE.
--
-- status lifecycle: 'pending' (link generated, not yet confirmed active) ->
-- 'active' (Composio confirmed the OAuth flow completed) or 'expired' (the
-- user never finished it, or Composio reported a terminal failure --
-- FAILED/EXPIRED/REVOKED). notified_at guards against double-texting the
-- same confirmation on a slow poll cycle, same role as
-- deepsearch_human_help_requests.notified_at.
--
-- users.email_connected is a cheap denormalized cache of "does this user
-- have an ACTIVE Gmail connection right now" -- read every turn (system
-- prompt / onboarding logic) without an extra Composio API round trip;
-- written only by mark_email_connected (poll loop) and never trusted as
-- the sole source of truth for actually calling a Gmail action (Composio's
-- own API response is), so it going stale for a few poll cycles is
-- harmless.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'email_connection_status') THEN
        CREATE TYPE email_connection_status AS ENUM ('pending', 'active', 'expired');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS email_connection_requests (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connected_account_id VARCHAR(64),  -- Composio's connection id (ca_xxx) once known
    status email_connection_status NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at TIMESTAMPTZ,           -- NULL until the poll loop has sent its one confirmation SMS
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_email_connection_requests_status ON email_connection_requests(status);
CREATE INDEX IF NOT EXISTS ix_email_connection_requests_user_id ON email_connection_requests(user_id);

ALTER TABLE users ADD COLUMN IF NOT EXISTS email_connected BOOLEAN NOT NULL DEFAULT FALSE;
