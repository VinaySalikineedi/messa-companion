-- Migration 010: Convert pending_actions.action_type to VARCHAR(100)
ALTER TABLE pending_actions ALTER COLUMN action_type TYPE VARCHAR(100) USING action_type::text;
