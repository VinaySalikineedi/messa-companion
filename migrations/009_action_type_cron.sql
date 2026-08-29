-- Migration 009: Add 'create_recurring_cron' to action_type ENUM
ALTER TYPE action_type ADD VALUE IF NOT EXISTS 'create_recurring_cron';
