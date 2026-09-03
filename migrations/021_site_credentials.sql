-- Additive migration: per-user, per-site credentials for accounts Messa
-- creates on the user's behalf via deepsearch (Browserbase + Playwright)
-- on sites Composio doesn't support (see migrations/020_dynamic_integrations.sql
-- and the "Non-API Web Automation Fallback" section of
-- docs/dynamic_tool_injection_spec.md).
--
-- Explicit product decision (asked and confirmed, not assumed): Messa
-- STORES these, encrypted, so she can log back into an account she
-- created without bothering the user again. The alternative considered --
-- generate a password, text it once, never persist it -- has a smaller
-- security surface but can't support that. This is the bigger-surface
-- option, chosen deliberately.
--
-- encrypted_password is application-level-encrypted (Fernet, see
-- messa/credentials.py) BEFORE it ever reaches this table -- Postgres
-- itself never sees a plaintext password, and a database dump/backup
-- alone is not enough to recover one; config.CREDENTIALS_ENCRYPTION_KEY
-- (kept OUTSIDE the database, in the deployment's own env/secrets) is
-- also required. This is deliberately NOT pgcrypto/column-level DB
-- encryption -- keeping the encrypt/decrypt step in application code
-- means the key is never anywhere the database itself could leak it from
-- (a misconfigured backup, a read replica, a leaked connection string).
--
-- UNIQUE(user_id, site_name): one stored credential per user per site.
-- db.save_site_credential inserts with ON CONFLICT DO NOTHING specifically
-- so a second generate_account_credential call for a site that already
-- has one can never silently overwrite it -- overwriting would leave the
-- STORED password out of sync with the REAL one on the actual site,
-- effectively locking Messa out. See db.py's own comment on that function.
--
-- site_name is a short slug/label the model chooses (e.g. 'airbnb',
-- 'united-mileageplus') -- deliberately NOT constrained to Composio's
-- toolkit-slug shape (migrations/020's app_connection_requests.toolkit_slug),
-- since this covers sites that by definition AREN'T on Composio at all.

CREATE TABLE IF NOT EXISTS site_credentials (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    site_name VARCHAR(128) NOT NULL,
    site_url TEXT,
    username VARCHAR(255) NOT NULL,
    encrypted_password TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    UNIQUE(user_id, site_name)
);
CREATE INDEX IF NOT EXISTS ix_site_credentials_user_id ON site_credentials(user_id);
