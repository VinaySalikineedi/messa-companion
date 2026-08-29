-- 1. Enable Required Extensions
CREATE EXTENSION IF NOT EXISTS vector;

-- 2. Create Custom ENUM Types
CREATE TYPE user_tier AS ENUM ('free', 'pro');
CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system');
CREATE TYPE reminder_status AS ENUM ('pending', 'sent', 'cancelled');
CREATE TYPE task_status AS ENUM ('todo', 'in_progress', 'done', 'cancelled');
CREATE TYPE task_priority AS ENUM ('low', 'medium', 'high', 'urgent');
CREATE TYPE calendar_event_status AS ENUM ('scheduled', 'cancelled', 'completed');
CREATE TYPE action_type AS ENUM (
    'create_task', 'update_task', 'delete_task', 
    'create_reminder', 'cancel_reminder', 
    'create_calendar_event', 'update_calendar_event', 'delete_calendar_event', 
    'delete_note', 'create_recurring_cron'
);
CREATE TYPE pending_action_state AS ENUM ('pending', 'confirmed', 'cancelled', 'expired');
CREATE TYPE cron_job_status AS ENUM ('active', 'paused', 'cancelled');

-- 3. Create Tables

-- Users Table
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    phone_number VARCHAR(20) UNIQUE NOT NULL,
    name VARCHAR(100),
    city VARCHAR(100),
    timezone VARCHAR(50) NOT NULL DEFAULT 'America/New_York',
    onboarding_step VARCHAR(30) NOT NULL DEFAULT 'awaiting_name',
    tier user_tier NOT NULL DEFAULT 'free',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_users_phone_number ON users(phone_number);

-- Message History Table
CREATE TABLE message_history (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role message_role NOT NULL,
    content TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_message_history_user_id ON message_history(user_id);
CREATE INDEX ix_message_history_timestamp ON message_history(timestamp);

-- Tasks Table
CREATE TABLE tasks (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    due_date TIMESTAMPTZ,
    status task_status NOT NULL DEFAULT 'todo',
    priority task_priority NOT NULL DEFAULT 'medium',
    external_provider_id VARCHAR(255),
    external_provider VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_tasks_user_id ON tasks(user_id);
CREATE INDEX ix_tasks_status ON tasks(status);
CREATE INDEX ix_tasks_priority ON tasks(priority);

-- Reminders Table
CREATE TABLE reminders (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    trigger_time TIMESTAMPTZ NOT NULL,
    message TEXT NOT NULL,
    status reminder_status NOT NULL DEFAULT 'pending',
    checkin_for_task_id INT REFERENCES tasks(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_reminders_user_id ON reminders(user_id);
CREATE INDEX ix_reminders_trigger_time ON reminders(trigger_time);
CREATE INDEX ix_reminders_status ON reminders(status);
CREATE INDEX ix_reminders_checkin_for_task_id ON reminders(checkin_for_task_id);

-- Calendar Events Table
CREATE TABLE calendar_events (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title VARCHAR(255) NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    location VARCHAR(500),
    notes TEXT,
    status calendar_event_status NOT NULL DEFAULT 'scheduled',
    external_provider_id VARCHAR(255),
    external_provider VARCHAR(50),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_calendar_events_user_id ON calendar_events(user_id);
CREATE INDEX ix_calendar_events_start_time ON calendar_events(start_time);
CREATE INDEX ix_calendar_events_status ON calendar_events(status);

-- Pending Actions Table (Approval State Machine)
CREATE TABLE pending_actions (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type VARCHAR(100) NOT NULL,
    payload TEXT NOT NULL, -- JSON serialized dict
    state pending_action_state NOT NULL DEFAULT 'pending',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX ix_pending_actions_user_id ON pending_actions(user_id);
CREATE INDEX ix_pending_actions_state ON pending_actions(state);
CREATE INDEX ix_pending_actions_expires_at ON pending_actions(expires_at);

-- Audit Logs Table
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type VARCHAR(100) NOT NULL,
    payload TEXT NOT NULL,
    source_pending_action_id INT REFERENCES pending_actions(id) ON DELETE SET NULL,
    executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX ix_audit_logs_executed_at ON audit_logs(executed_at);
CREATE INDEX ix_audit_logs_source_pending_action_id ON audit_logs(source_pending_action_id);

-- Vector Memories Table (pgvector Semantic Memory)
CREATE TABLE vector_memories (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    embedding vector(1536), -- 1536 dimensions for text-embedding-004
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_vector_memories_user_id ON vector_memories(user_id);

-- Notes Table
CREATE TABLE notes (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    tags VARCHAR(255),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_notes_user_id ON notes(user_id);
CREATE INDEX ix_notes_created_at ON notes(created_at);

-- People (Contacts Context) Table
CREATE TABLE people (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(100) NOT NULL,
    relationship_type VARCHAR(50),
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_people_user_name UNIQUE (user_id, name)
);
CREATE INDEX ix_people_user_id ON people(user_id);
CREATE INDEX ix_people_name ON people(name);

-- Cron Jobs Table (Recurring Briefings & Automations)
CREATE TABLE cron_jobs (
    id SERIAL PRIMARY KEY,
    user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    prompt_or_task TEXT NOT NULL,
    cron_expression VARCHAR(100) NOT NULL,
    user_timezone VARCHAR(50) NOT NULL DEFAULT 'America/New_York',
    next_run_at TIMESTAMPTZ NOT NULL,
    last_run_at TIMESTAMPTZ,
    status cron_job_status NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ix_cron_jobs_user_id ON cron_jobs(user_id);
CREATE INDEX ix_cron_jobs_next_run_at ON cron_jobs(next_run_at);
CREATE INDEX ix_cron_jobs_status ON cron_jobs(status);
