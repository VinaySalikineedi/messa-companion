-- Migration 037: Persistent Workspace Asset Registry + per-toolkit Entity
-- cache (docs/executive_agent_architecture_proposal.md, sections 3.A/3.B),
-- built on top of feature/agentic-upgrade (migration 033's active_tasks/
-- agent_skills).
--
-- active_tasks (migration 033) is ONE task's own working memory -- its
-- artifacts are wiped/dropped once that task finishes (see that
-- migration's own retention comment). agent_skills (migration 033) is a
-- GLOBAL, cross-user playbook of tool/site lessons -- never about one
-- user's own stuff. Neither covers the actual gap this migration closes:
-- a PER-USER, PERSISTENT memory of the real-world things Messa has
-- created or touched for them (a Google Sheet, an Airtable base, a
-- generated PDF, ...) that should still be findable next week, long
-- after the task that created it is gone -- the concrete failure mode
-- this fixes is Messa forgetting she already made someone a spreadsheet
-- last Tuesday and asking them to go dig it out of Google Drive herself.
--
-- Two tables, two separate jobs, same SERIAL-id-per-user shape as
-- migration 020's user_app_preferences (NOT migration 033's UUID
-- convention -- these are closer kin to that table: one user, many rows,
-- always queried by (user_id, ...), never joined across users):
--
-- 1. user_assets -- the registry itself. UNIQUE(user_id, asset_type,
-- external_id) is the dedup key: re-touching an already-known asset
-- (db.record_user_asset) just refreshes title/url/summary and bumps
-- last_referenced_at on the SAME row via ON CONFLICT DO UPDATE, instead
-- of piling up duplicate rows every time the user asks about (or Messa
-- re-opens) the same spreadsheet. external_id is nullable because not
-- every discovery path has one (the background "sleep & dream" sweep in
-- asset_consolidation.py only ever has a bare URL) -- Postgres's own
-- UNIQUE-constraint semantics already do the right thing here (NULL is
-- never equal to NULL, so those inserts never collide against the
-- constraint), which matches the real-world semantics too: two
-- unknown-id discoveries of the same type/title genuinely might be two
-- different things, not the same row touched twice, so they're kept
-- separate rather than forced to dedup on a guess.
CREATE TABLE IF NOT EXISTS user_assets (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    asset_type VARCHAR(64) NOT NULL,   -- e.g. 'airtable_base', 'googlesheets_spreadsheet', 'pdf_table'
    title TEXT NOT NULL,
    external_id VARCHAR(255),          -- the third-party id (spreadsheet id, base id, ...), when known
    url TEXT,
    summary TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_referenced_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, asset_type, external_id)
);

-- The one query shape this table exists to serve fast --
-- registry._build_system_prompt injecting "the user's N most recently
-- touched assets" on every single turn -- so it gets its own covering
-- index rather than relying on a table scan plus sort.
CREATE INDEX IF NOT EXISTS ix_user_assets_recent ON user_assets (user_id, last_referenced_at DESC);

-- 2. user_app_entities -- a small per-(user, toolkit, entity_type) cache
-- of a default id Messa would otherwise have to ask the user for (a
-- default Airtable workspace, a default Google Drive folder, ...).
-- Discovered once -- see tools/integration_tools.py's
-- discover_and_cache_app_entities, called from server.py's app-connection
-- poll loop right after a connection goes ACTIVE, and opportunistically
-- again from execute_integration_tool for a connection that predates this
-- feature -- and reused silently from then on. UNIQUE(user_id,
-- toolkit_slug, entity_type) is the whole dedup mechanism, upserted the
-- same way as migration 020's user_app_preferences.
CREATE TABLE IF NOT EXISTS user_app_entities (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    toolkit_slug VARCHAR(64) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,  -- e.g. 'workspace_id', 'folder_id'
    entity_id VARCHAR(255) NOT NULL,
    label TEXT,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, toolkit_slug, entity_type)
);
CREATE INDEX IF NOT EXISTS ix_user_app_entities_lookup ON user_app_entities (user_id, toolkit_slug, entity_type);
