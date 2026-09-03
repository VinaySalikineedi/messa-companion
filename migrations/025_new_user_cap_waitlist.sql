-- Migration 025: new-user signup cap + waitlist.
--
-- Purely additive, off by default: nothing here changes behavior unless
-- MESSA_NEW_USER_CAP (messa/config.py) is set above 0. With it unset (the
-- current, existing-app default), messa/waitlist.py's gate short-circuits
-- before touching either table below, so this migration is safe to run
-- against the live database at any time regardless of whether the env var
-- is set yet.
--
-- app_settings: a small generic key/value store (this is its first use --
-- the new-user cap's auto-captured baseline id below). Not scoped to any
-- one feature on purpose, so a future runtime setting has somewhere to
-- live without another migration.
CREATE TABLE IF NOT EXISTS app_settings (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- waitlist: one row per phone number that texted in while the cap was
-- full. `admitted_at` (set by the new admin `admit_from_waitlist` tool) is
-- the escape hatch for letting a specific person in past the cap -- it
-- doesn't create their `users` row itself, it just tells
-- messa/waitlist.py's gate to let their NEXT inbound text through to the
-- normal get_or_create_user path instead of re-waitlisting them.
CREATE TABLE IF NOT EXISTS waitlist (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at TIMESTAMPTZ,
    admitted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_waitlist_phone_number ON waitlist(phone_number);
