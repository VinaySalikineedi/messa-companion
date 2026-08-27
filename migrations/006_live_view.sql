-- Additive migration: per-user "live view" sharing -- one permanent,
-- unguessable link per user (users.live_share_token) that Messa texts
-- alongside every deepsearch delegation ("watch it live here: <link>").
-- The link never changes, so it only ever needs to be generated once and
-- reused forever -- it's the URL that matters, not the token's freshness.
--
-- users.live_view_url / live_view_task / live_view_started_at track
-- "is a browser open for this user *right now*" -- set the instant
-- BrowserToolProvider opens a Browserbase session, cleared the instant
-- that delegation is done with it (completed, hit its step limit, or
-- errored -- all three close the browser, see tools/deepsearch_tools.py's
-- try/finally). This is deliberately separate from deepsearch_sessions,
-- which tracks completed/resumable run *history* rather than live state --
-- a session can be sitting there as "active" (resumable) for hours after
-- its browser already closed, so that table alone can't answer "is
-- something happening on screen at this exact moment," which is the only
-- question the live-view page needs answered.

ALTER TABLE users ADD COLUMN IF NOT EXISTS live_share_token VARCHAR(64) UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS live_view_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS live_view_task TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS live_view_started_at TIMESTAMPTZ;
