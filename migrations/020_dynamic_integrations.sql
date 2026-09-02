-- Additive migration: Messa's Dynamic Integration Engine (see
-- docs/dynamic_tool_injection_spec.md) -- letting Messa discover, connect,
-- and use any of Composio's 1,400+ app toolkits per-user, on top of the
-- Gmail-specific integration migrations/012_email_connection.sql already
-- built (which this deliberately leaves untouched -- see
-- tools/email_tools.py's own docstring for why Gmail stays its own
-- special case rather than being folded into this generic path).
--
-- Three tables, three separate jobs:
--
-- 1. user_app_preferences -- which connected app a user wants used by
--    default for a given category (e.g. 'tasks' -> 'todoist') when more
--    than one connected app could plausibly serve the same request.
--    VARCHAR rather than an enum for app_category/preferred_app on
--    purpose: Composio's catalog is 1,400+ toolkits and growing, an enum
--    would need a migration every time a new one showed up.
--
-- 2. app_connection_requests -- same decoupled shape as
--    migrations/012_email_connection.sql's email_connection_requests
--    (and migrations/008_deepsearch_human_help.sql before that):
--    tools/integration_tools.py's connect_integration_app creates a row
--    the moment it generates a Composio connect link and returns
--    immediately (never blocks the conversation on an OAuth flow that
--    might take anywhere from seconds to hours); server.py's
--    _production_app_connection_poll_loop is a separate background task
--    that polls Composio for that row's real status and sends exactly one
--    confirmation text once it goes ACTIVE. Deliberately generalized
--    rather than a copy-per-app: unlike Gmail (which gets its own cached
--    users.email_connected boolean, read every turn without a Composio
--    round trip, because it's one specific, always-relevant integration),
--    an arbitrary toolkit's connection status is checked live via
--    Composio's connected_accounts API when actually needed
--    (search_integration_tools), not cached per-app on the users row --
--    caching would mean one new column per toolkit, unbounded.
--
-- 3. unsupported_integration_requests -- logs every query
--    search_integration_tools couldn't match to any Composio toolkit at
--    all, so there's a real, ranked signal for which integrations to
--    prioritize next -- independent of whether the deepsearch/Browserbase
--    fallback then managed to handle the request anyway.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'app_connection_status') THEN
        CREATE TYPE app_connection_status AS ENUM ('pending', 'active', 'expired');
    END IF;
END$$;

CREATE TABLE IF NOT EXISTS user_app_preferences (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    app_category VARCHAR(64) NOT NULL,   -- e.g. 'email', 'tasks', 'calendar'
    preferred_app VARCHAR(64) NOT NULL,  -- e.g. 'todoist', 'gmail', 'messa'
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(user_id, app_category)
);
CREATE INDEX IF NOT EXISTS ix_user_app_preferences_user_id ON user_app_preferences(user_id);

CREATE TABLE IF NOT EXISTS app_connection_requests (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    toolkit_slug VARCHAR(64) NOT NULL,        -- e.g. 'todoist', 'slack', 'notion'
    connected_account_id VARCHAR(64),         -- Composio's connection id (ca_xxx) once known
    status app_connection_status NOT NULL DEFAULT 'pending',
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notified_at TIMESTAMPTZ,                  -- NULL until the poll loop has sent its one confirmation SMS
    resolved_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS ix_app_connection_requests_status ON app_connection_requests(status);
CREATE INDEX IF NOT EXISTS ix_app_connection_requests_user_id ON app_connection_requests(user_id);

CREATE TABLE IF NOT EXISTS unsupported_integration_requests (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    requested_app_name VARCHAR(128) NOT NULL,
    raw_user_prompt TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS ix_unsupported_integration_requests_app ON unsupported_integration_requests(requested_app_name);
