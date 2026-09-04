-- Additive migration: durable state for the email-verification-code relay
-- (deepsearch signs up for a site using the user's own Messa-assigned
-- inbox -- generate_account_credential's default username, see
-- tools/deepsearch_tools.py -- hits an OTP/verification screen, and needs
-- the code the SITE just emailed to that Messa inbox routed back into the
-- waiting browser tab). This is a narrower, more automatable cousin of
-- deepsearch_human_help_requests (migrations/008): a real human can't help
-- here even in principle, since the code goes to an inbox only Messa can
-- read -- so instead of waiting for a person to take over the live view,
-- server.py's personal-email webhook resolves this row itself the moment
-- the verification email arrives, and the in-flight deepsearch tool call
-- (BrowserToolProvider._await_email_verification_code) picks it up on its
-- next poll -- same poll-a-durable-row shape as human_help, just resolved
-- by an inbound-email webhook instead of a person acting in the live view.
--
-- status lifecycle: 'pending' (just registered, tab holding its browser
-- session open while it waits) -> 'resolved' (a matching inbound email
-- supplied a code) or 'expired' (the waiting tool call gave up after its
-- own bounded timeout -- see config.DEEPSEARCH_OTP_WAIT_MAX_SECONDS --
-- and marked its own row so a later, unrelated email can't be mistaken
-- for an answer to a session that already moved on).
--
-- sender_filter is optional (the model passes whatever short keyword it
-- can infer about the expected sender, e.g. 'uber') -- used to prefer the
-- right expectation when a user happens to have more than one pending at
-- once (plausible now that DEEPSEARCH_MAX_CONCURRENT_SESSIONS allows many
-- concurrent top-level sessions); never required, since not every signup
-- flow gives the model a confident guess up front.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'otp_expectation_status') THEN
        CREATE TYPE otp_expectation_status AS ENUM ('pending', 'resolved', 'expired');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS deepsearch_otp_expectations (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    deepsearch_session_id INT REFERENCES deepsearch_sessions(id) ON DELETE CASCADE,
    tab_marker VARCHAR(64) NOT NULL,   -- correlates to the specific browser tab, same convention as deepsearch_human_help_requests.tab_marker
    sender_filter VARCHAR(128),        -- short keyword the model expects in the from-address/subject (e.g. 'uber'); NULL/blank = no preference
    code VARCHAR(32),                  -- the extracted verification code, set only once status = 'resolved'
    status otp_expectation_status NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_deepsearch_otp_expectations_pending ON deepsearch_otp_expectations(user_id, status);
