-- Additive migration: durable state for the human-in-the-loop pause flow
-- (deepsearch hits a login wall/CAPTCHA/2FA, asks for your help, and needs
-- to reliably notify you and give up on a bounded timer even if the
-- in-flight agent call itself gets interrupted). Deliberately a real table,
-- not something kept only in memory (like live_activity.py's per-user
-- state) -- the notification step is a SEPARATE poll loop
-- (server.py's _production_deepsearch_pause_loop), decoupled from the
-- in-process deepsearch agent call that created the row, and needs
-- something durable to poll.
--
-- status lifecycle: 'waiting' (just requested, not yet resolved) ->
-- 'resolved' (activity detected, page moved past the blocker) or
-- 'timed_out' (no useful activity within the bounded window -- see
-- config.DEEPSEARCH_HUMAN_HELP_* constants). notified_at is set the first
-- time the poll loop sends the one Sendblue SMS for this row, so a slow
-- poll cycle or a retry never double-texts you.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'human_help_status') THEN
        CREATE TYPE human_help_status AS ENUM ('waiting', 'resolved', 'timed_out');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS deepsearch_human_help_requests (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    deepsearch_session_id INT REFERENCES deepsearch_sessions(id) ON DELETE CASCADE,
    tab_marker VARCHAR(64) NOT NULL,  -- correlates to the specific browser tab (see deepsearch_tools.py)
    reason TEXT NOT NULL,             -- the model's own explanation (e.g. "login form, needs your password")
    status human_help_status NOT NULL DEFAULT 'waiting',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at TIMESTAMPTZ,          -- NULL until the poll loop has sent its one SMS for this row
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_deepsearch_human_help_status ON deepsearch_human_help_requests(status);
CREATE INDEX IF NOT EXISTS ix_deepsearch_human_help_user_id ON deepsearch_human_help_requests(user_id);
