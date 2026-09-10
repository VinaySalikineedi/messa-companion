-- Migration 038: Deterministic User Lists (V3-autonomous.md Pillar 3),
-- built on top of feature/agentic-upgrade's migration 037.
--
-- The field incident this fixes: asked to mute a marketing sender
-- (Metricool), Messa had no list store at all, so she improvised a
-- 30-minute recurring cron job that re-checked the inbox 48 times a day
-- just to keep ignoring the same domain -- expensive, slow to take
-- effect, and a routines-list entry the user never actually wanted to see.
--
-- Deliberately NOT bundled with V3-autonomous.md's own user_policies
-- table in this migration: that's a much bigger, general "declarative
-- condition -> action" rule engine, and nothing in Phase 2's own listed
-- deliverables (manage_user_list-equivalent tools + the zero-token
-- webhook filter) actually reads from it yet -- Phase 3's VIP/priority
-- classifier is the first real consumer. Shipping an unused table now
-- would be exactly the kind of speculative schema this codebase's own
-- migration philosophy avoids elsewhere; add it in the phase that
-- actually builds something against it instead.
--
-- One deliberately GENERIC table (same SERIAL-id-per-user shape as
-- migration 020's user_app_preferences / migration 037's user_assets),
-- so a second list type (vip_contacts, preferred_airlines, ...) is just
-- new consumer code against the same schema, not a new migration:
CREATE TABLE IF NOT EXISTS user_lists (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    list_name VARCHAR(100) NOT NULL,   -- 'muted_email_senders' is the only consumer today
    item_value TEXT NOT NULL,          -- normalized (lowercased/trimmed) before storage -- see db.add_user_list_item
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, list_name, item_value)
);

-- The one query shape this exists to serve fast -- db.is_muted_sender
-- runs on EVERY inbound personal-email webhook call, before deciding
-- whether to spend an LLM turn notifying the user at all.
CREATE INDEX IF NOT EXISTS ix_user_lists_lookup ON user_lists (user_id, list_name);
