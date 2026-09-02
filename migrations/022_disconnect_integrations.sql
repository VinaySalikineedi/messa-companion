-- Additive migration: lets Messa herself disconnect a connected app or
-- Gmail account, at the user's own explicit request, instead of only ever
-- being able to connect one. Fixes a real reported bug: with no
-- disconnect/switch capability, Messa (asked to switch Google Calendar to
-- a different account) fabricated a fictional "go to Messa's
-- connection/integrations settings page" instruction -- no such page
-- exists in this product. See tools/integration_tools.py's new
-- disconnect_integration_app tool and connect_integration_app's new
-- switch_account param, and tools/email_tools.py's new disconnect_email
-- tool and request_email_connection's new switch_account param.
--
-- New status value 'disconnected', distinct from the existing 'expired':
-- 'expired' already means "a pending OAuth link timed out, or Composio
-- reported a terminal failure (FAILED/EXPIRED/REVOKED) before the user
-- ever finished connecting" -- a different, EARLIER-in-the-lifecycle event
-- from "a previously ACTIVE, working connection was deliberately
-- disconnected." Keeping these separate keeps the status column an
-- honest, unambiguous record of what actually happened to a row, rather
-- than overloading 'expired' to mean two genuinely different things.
--
-- ADD VALUE IF NOT EXISTS, same safe-to-re-run pattern as migrations/
-- 009_action_type_cron.sql's identical use on a different enum -- each
-- migration file already runs as its own single `psql -f` invocation (see
-- README's setup section), so this needs no special transaction handling.

ALTER TYPE app_connection_status ADD VALUE IF NOT EXISTS 'disconnected';
ALTER TYPE email_connection_status ADD VALUE IF NOT EXISTS 'disconnected';
