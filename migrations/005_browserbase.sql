-- Additive migration: switches deepsearch from a local Chromium (spawned
-- inside our own container) to Browserbase-hosted remote browser sessions.
--
-- Why: two rounds of "Chrome isn't installed" failures on the HF Space
-- traced back to real, fixable bugs (MCP not inheriting the parent
-- environment; a --browser flag mismatch) -- but every one of them was a
-- symptom of running a real Chromium inside a constrained, ephemeral
-- container. Browserbase runs the actual browser on its own infrastructure;
-- our container just drives it over CDP. This also directly sets up
-- Phase 3 (an interactive live-view link the user can open) since
-- Browserbase's Live View is exactly that, and gives per-user login
-- persistence that (unlike the old --user-data-dir profile directory)
-- survives an HF Space restart, since it isn't stored on the container's
-- ephemeral disk at all.
--
-- users.browserbase_context_id: one Browserbase Context per user, created
-- once and reused forever (Contexts live indefinitely on Browserbase's
-- side until explicitly deleted) -- this is the direct replacement for the
-- old local deepsearch_profiles/user-<id> directory.
--
-- deepsearch_sessions.live_view_url: captured every time a deepsearch run
-- starts, so it's already sitting in the DB, ready for whenever Messa
-- starts texting it to users as the Phase 3 "watch live" link -- not wired
-- into any reply yet, just captured.

ALTER TABLE users ADD COLUMN IF NOT EXISTS browserbase_context_id VARCHAR(255);

ALTER TABLE deepsearch_sessions ADD COLUMN IF NOT EXISTS live_view_url TEXT;
