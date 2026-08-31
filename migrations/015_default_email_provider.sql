-- Additive migration: lets the user choose which inbox Messa treats as the
-- default for a generic "send/check my email" request that doesn't name
-- one -- 'messa' (their own <local-part>@config.TEXTMESSA_EMAIL_DOMAIN
-- address, migrations/013_personal_email.sql) or 'gmail' (their connected
-- Gmail via Composio, migrations/012_email_connection.sql).
--
-- The NOT NULL ... DEFAULT 'messa' clause is doing double duty on purpose,
-- per explicit product decision ("even for existing users now, make
-- messa's email the default, and the same for anyone who onboards from
-- now on"):
--   1. Every EXISTING row gets backfilled to 'messa' the moment this
--      migration runs -- Postgres applies a constant DEFAULT to already-
--      existing rows as part of the ADD COLUMN itself (a fast, metadata-
--      only operation on PG 11+, no separate UPDATE needed).
--   2. Every user created AFTER this migration also starts at 'messa' --
--      db.get_or_create_user's plain INSERT never has to mention this
--      column at all for that to hold.
-- So there is deliberately no backfill script/UPDATE statement here beyond
-- the ADD COLUMN itself -- both halves of the ask fall out of one clause.
--
-- The only thing that ever changes this column after the fact is the
-- user's own explicit request ("use my gmail as default", "switch back to
-- messa") via agents/registry.py's set_default_email_provider tool --
-- connecting Gmail (mark_email_connected) does NOT flip this on its own,
-- same "the user decides, nothing switches automatically" shape as this
-- project's card/top-up decision earlier on. Safe to re-run; the app also
-- works without this applied yet -- db.py checks _has_column first and the
-- config.UserContext.default_email_provider field itself already defaults
-- to "messa" in Python, so pre-migration behavior is identical to
-- post-migration behavior for every user, just not persisted/switchable
-- yet.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'email_provider') THEN
        CREATE TYPE email_provider AS ENUM ('messa', 'gmail');
    END IF;
END$$;

ALTER TABLE users ADD COLUMN IF NOT EXISTS default_email_provider email_provider NOT NULL DEFAULT 'messa';
