-- V3-autonomous.md Phase 5 ("Meeting Dossiers & Commitment Ledger").
--
-- Two new, independent tables -- see messa/commitments.py and
-- messa/meeting_dossiers.py's own module docstrings for the full design.
-- Both follow this project's established additive/no-op-safe convention
-- (db.py guards every read/write with _has_table, so a deployment that
-- hasn't run this migration yet just sees the feature as a no-op, same as
-- migrations/039_email_digest_queue.sql before it).

-- user_commitments: an implicit-promise ledger ("I'll send the updated
-- deck by Thursday"), populated by one isolated, bounded LLM call
-- (messa/commitments.py's extract_commitment, same shape as
-- messa/media_understanding.py) run AFTER a real outbound email send
-- succeeds (messa/tools/email_tools.py + personal_inbox_tools.py's
-- send_email/reply_to_email) -- never before, never blocking the send
-- itself. Adapted from V3-autonomous.md section 5's proposed schema, not
-- copied verbatim: added source_excerpt (the sentence(s) the classifier
-- keyed off, so a user can sanity-check or dismiss a wrong guess instead
-- of trusting an opaque summary) and nudge_sent_at/updated_at (delivery
-- dedup + audit, the same shape reminders/email_digest_queue already use)
-- -- the doc's bare schema had no delivery-dedup story at all.
CREATE TABLE IF NOT EXISTS user_commitments (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    counterparty_name VARCHAR(255),
    counterparty_email VARCHAR(255),
    commitment_summary TEXT NOT NULL,
    source_excerpt TEXT,
    due_date DATE,
    source_type VARCHAR(50) NOT NULL DEFAULT 'OUTBOUND_EMAIL',
    status VARCHAR(50) NOT NULL DEFAULT 'PENDING',
    nudge_sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_commitments_user_status ON user_commitments(user_id, status);
-- Used by the due-date nudge poller (server.py's
-- _production_commitment_nudge_loop): only ever scans PENDING commitments
-- with a due_date that haven't been nudged yet.
CREATE INDEX IF NOT EXISTS ix_commitments_due_nudge
    ON user_commitments(due_date)
    WHERE status = 'PENDING' AND nudge_sent_at IS NULL;

-- meeting_dossier_events: a generic send-dedup ledger for the T-10-minute
-- pre-meeting brief and T+3-minute post-meeting voice-note prompt
-- (messa/meeting_dossiers.py). Deliberately NOT two new columns on
-- calendar_events -- a connected calendar's events (Google Calendar via
-- Composio, watched when that's the user's app-preference primary
-- calendar -- see db.get_app_preference) have no row in THIS database at
-- all to attach a sent-flag column to, and the whole point of Phase 5's
-- calendar-source design is that native and connected events are polled
-- through the exact same code path. event_key is 'native:<calendar_
-- events.id>' for Messa's own calendar or '<toolkit>:<external event
-- id>' (e.g. 'googlecalendar:abc123') for a connected one -- see
-- meeting_dossiers.py's _event_key.
CREATE TABLE IF NOT EXISTS meeting_dossier_events (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_key VARCHAR(500) NOT NULL,
    pre_brief_sent_at TIMESTAMPTZ,
    post_harvest_sent_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, event_key)
);

-- pending_post_meeting_notes: a short-lived (see
-- config.POST_MEETING_CONTEXT_TTL_SECONDS), single-row-per-user context
-- flag set the moment the T+3 voice-note prompt above goes out, and read
-- by agents/registry.py's system prompt builder so the very next inbound
-- message from that user (a voice memo transcript, or a typed recap) gets
-- treated as meeting notes worth turning into drafted follow-ups/intros/
-- CRM notes, instead of an ordinary message. Cleared the moment it's been
-- surfaced once (see db.pop_pending_post_meeting_note) or once it expires
-- -- deliberately NOT a queue/history table, since only the most recent
-- meeting's context is ever relevant to "what did the user just say."
CREATE TABLE IF NOT EXISTS pending_post_meeting_notes (
    user_id INT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    event_title VARCHAR(255),
    counterparty_name VARCHAR(255),
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
