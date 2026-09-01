-- Additive migration: lets a locally-generated file (currently always a
-- PDF from tools/document_tools.py's generate_pdf) be exposed at a public,
-- per-file, unguessable-token URL (server.py's GET /files/{token}) so it
-- can be attached to an outbound TEXT message via Sendblue's media_url.
-- Sendblue's API needs a URL it can fetch itself, not raw bytes -- a
-- constraint that doesn't exist for outbound EMAIL (channels/resend.py
-- base64-encodes the file directly into the request), which is why this
-- table has no email equivalent.
--
-- token is generated the same way as users.live_share_token
-- (secrets.token_urlsafe(24)) -- unguessable, not sequential/enumerable.
-- No expiry column: a share is only ever created for a file the user just
-- asked to have texted, so it's short-lived by nature (Sendblue fetches it
-- once, right away); server.py's route re-validates file_path against
-- config.OUTPUTS_DIR at SERVE time too, not just at creation time, so even
-- a long-lived stale row can never serve a file that's since moved outside
-- the outputs directory.

CREATE TABLE IF NOT EXISTS generated_document_shares (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token VARCHAR(64) UNIQUE NOT NULL,
    file_path TEXT NOT NULL,
    filename VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_generated_document_shares_token ON generated_document_shares(token);
