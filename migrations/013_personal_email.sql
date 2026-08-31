-- Additive migration: gives each user their own address on Messa's domain
-- (<local-part>@config.TEXTMESSA_EMAIL_DOMAIN, "textmessa.com" by default --
-- see messa/tools/personal_inbox_tools.py), separate from both their
-- personal Gmail (migrations/012_email_connection.sql) and their optional
-- contact email on file (migrations/003_user_email.sql). Safe to re-run;
-- the app also works without this applied yet -- db.py checks
-- _has_column/_has_table first and just reports "not set up yet" until you
-- run this, same pattern as every other migration here.
--
-- messa_email_local_part is assigned automatically (see
-- db.get_or_create_messa_email_local_part, called from cli.load_user_context
-- on every turn like get_or_create_live_share_token) -- there's no
-- onboarding question for it, every user just gets one the first time their
-- context loads. The partial unique index (WHERE ... IS NOT NULL) is what
-- lets many rows sit at NULL simultaneously (before this migration is
-- applied, or for any row created in the brief window before provisioning
-- runs) while still enforcing uniqueness on every local part that IS set.
--
-- inbound_personal_emails is a dedup log, not a mailbox: Cloudflare Email
-- Routing's Worker (cloudflare/personal-email-worker/) has no built-in
-- retry/dedup of its own, and a flaky POST to our webhook could plausibly
-- get retried by whatever's in front of it -- so message_id (the email's
-- own RFC 5322 Message-ID header, globally unique per message) is UNIQUE
-- here, and server.py's /webhooks/personal-email/inbound uses an
-- ON CONFLICT DO NOTHING insert to recognize a redelivery and skip
-- re-processing it (no second agent turn, no second SMS about the same
-- email) instead of trying to de-duplicate after the fact.

ALTER TABLE users ADD COLUMN IF NOT EXISTS messa_email_local_part VARCHAR(64);
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_messa_email_local_part
    ON users (messa_email_local_part) WHERE messa_email_local_part IS NOT NULL;

CREATE TABLE IF NOT EXISTS inbound_personal_emails (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id VARCHAR(998) NOT NULL,  -- RFC 5322's own max header line length
    from_address VARCHAR(320) NOT NULL,
    subject TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_inbound_personal_emails_message_id UNIQUE (message_id)
);
CREATE INDEX IF NOT EXISTS ix_inbound_personal_emails_user_id ON inbound_personal_emails(user_id);
