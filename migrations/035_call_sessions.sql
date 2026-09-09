-- Migration 035: call_sessions -- outbound voice-calling feature
-- (plans/glowing-forging-pumpkin.md). Messa can place a real outbound
-- phone call on the user's behalf via a third-party voice provider
-- (Vapi AI, messa/channels/vapi.py) and this table is the durable record
-- of each one: what was asked for, what's allowed to be disclosed mid-
-- call, and how it ended.
--
-- Two deliberate omissions, called out here so a future reader doesn't
-- assume they were forgotten:
--   * No raw full-transcript column -- only a scrubbed outcome_summary.
--     A full transcript is a bigger at-rest sensitivity surface than v1
--     needs to take on; add transcript_scrubbed TEXT in a follow-up
--     migration if a real audit/dispute need comes up later.
--   * No listen_url column -- Vapi's listenUrl is a bearer-of-the-url-has-
--     access credential-shaped string. It's held only in the in-memory
--     call_activity registry (messa/call_activity.py), same "nothing
--     meaningful survives a restart anyway" reasoning live_activity.py
--     already uses for its own in-flight state, and it minimizes at-rest
--     exposure of that URL.
CREATE TABLE IF NOT EXISTS call_sessions (
    id                    SERIAL PRIMARY KEY,
    call_id               TEXT NOT NULL,              -- app-generated uuid4, correlates with pending_actions payload
    user_id               INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    pending_action_id     INT REFERENCES pending_actions(id),
    provider              VARCHAR(20) NOT NULL DEFAULT 'vapi',
    provider_call_id      VARCHAR(128),               -- NULL until Vapi's create-call response arrives
    destination_number    VARCHAR(32) NOT NULL,
    business_name         VARCHAR(255),
    task_description      TEXT NOT NULL,
    scratchpad_snapshot   TEXT NOT NULL DEFAULT '{}',  -- JSON text, frozen at confirm time, scrubbed -- see call_tools.py
    allowed_info_fields   TEXT NOT NULL DEFAULT '[]',  -- JSON array: the closed allowlist the mid-call info tool may ever answer from
    status                VARCHAR(20) NOT NULL DEFAULT 'confirmed',
        -- confirmed -> dialing -> ringing -> in_progress -> ended | failed
    max_duration_seconds  INT NOT NULL,
    started_at            TIMESTAMPTZ,
    ended_at              TIMESTAMPTZ,
    duration_seconds      INT,
    minutes_billed        NUMERIC(6,2),
    outcome               VARCHAR(20),                -- success/failed/no_answer/voicemail/declined_by_agent/error
    outcome_summary       TEXT,                       -- a scrubbed short summary, never a raw transcript
    error_message         TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_call_sessions_user_status ON call_sessions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_call_sessions_provider_call_id ON call_sessions(provider_call_id);
