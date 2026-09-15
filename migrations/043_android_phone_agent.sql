-- Migration 043: Open-Source Phone / Bring Your Own Phone (BYOP)
-- (open-source-phone.md, feature/open-source-phone)
--
-- Pairs a user's own Android phone over ADB wireless debugging
-- (adbutils/uiautomator2, messa/devices/android.py) so Messa can drive it
-- like a human would: open apps, order food, book rides, navigate
-- dashboards. Three new tables, feature-flagged (config.
-- ANDROID_PHONE_AGENT_ENABLED, default off) and shipped on its own branch
-- precisely so it can be paired and tested hard against a REAL phone
-- before touching main/production -- same "own branch, own flag" shape
-- as migration 033 (scratchpad/skills) and 042 (grocery_agent) before it.
--
-- Deliberately does NOT add a new table for "the current mid-task
-- checkpoint a phone task is paused on" -- that reuses the EXISTING
-- active_tasks table from migration 033 (task_type = 'android_phone_agent'),
-- the same Active Task Scratchpad mechanism light-web-agent's own human-
-- checkpoint resume flow already relies on (see messa/channels/
-- light_web_agent.py's _persist_pending_checkpoint). Nor does it touch
-- agent_skills (migration 033) -- that table stays the automatic, internal,
-- cross-user "how does this app/site actually behave" learning cache
-- (agent_type = 'android_phone_agent' rows live there too, same as every
-- other agent). device_skills below is a DIFFERENT, deliberate product
-- surface: a skill a user explicitly chose to publish, with author
-- attribution and a public/private flag, meant to be browsed on a public
-- showcase page (messa.ai/skills) -- conflating the two would leak every
-- automatically-learned internal recipe onto a public page nobody asked
-- to publish it to.

-- ---------------------------------------------------------------------------
-- 1. user_devices -- one row per Android phone a user has paired.
-- tunnel_host/tunnel_port are the device's LAST KNOWN wireless-debugging
-- "connect" address (distinct from the ephemeral pairing port shown once
-- in Developer Options -- that one is used only for the one-time `adb
-- pair` handshake and is never stored here). A device's IP/port routinely
-- changes on Wi-Fi reconnect (see open-source-phone.md section 3's own
-- "IP / Port Changed" diagnostic case) -- messa/devices/android.py updates
-- this row whenever the user re-pairs or re-connects with a fresh
-- address, it's never assumed stable.
CREATE TABLE IF NOT EXISTS user_devices (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name VARCHAR(100) NOT NULL,          -- e.g. "Alice's Pixel 8"
    tunnel_host VARCHAR(255) NOT NULL,
    tunnel_port INTEGER NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'paired',  -- paired | connected | busy | offline | revoked
    last_seen_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, device_name)
);
CREATE INDEX IF NOT EXISTS ix_user_devices_user ON user_devices (user_id);

-- ---------------------------------------------------------------------------
-- 2. device_authorizations -- family sharing (open-source-phone.md section
-- 4): the phone owner names another contact (email or E.164 phone) who may
-- queue tasks against their device, scoped to an allowlist of app
-- categories (never a specific app list -- "Food delivery, Rides, Smart
-- Home, Streaming, Maps" per the doc; banking/messaging/photos/settings
-- are never authorizable here at all -- see config.
-- ANDROID_PHONE_BLOCKED_CATEGORIES, enforced in code, not the DB, so the
-- blocklist can't be accidentally widened by inserting a row). The device
-- OWNER themselves needs no row here -- authorization is only checked for
-- a requester who isn't user_devices.user_id.
CREATE TABLE IF NOT EXISTS device_authorizations (
    id SERIAL PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES user_devices(id) ON DELETE CASCADE,
    authorized_contact VARCHAR(255) NOT NULL,   -- email or E.164 phone
    allowed_categories TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (device_id, authorized_contact)
);
CREATE INDEX IF NOT EXISTS ix_device_authorizations_contact ON device_authorizations (authorized_contact);
CREATE INDEX IF NOT EXISTS ix_device_authorizations_device ON device_authorizations (device_id);

-- ---------------------------------------------------------------------------
-- 3. device_skills -- the EXPLICIT community skill registry (open-source-
-- phone.md section 6), separate from the automatic agent_skills cache (see
-- header comment above for why). A row is only ever created by a
-- deliberate "publish this skill" action (tools/android_phone_tools.py's
-- publish_phone_skill), never written to automatically -- is_public
-- defaults FALSE so a freshly-learned recipe is private to its author
-- until they explicitly choose to share it. recipe_json is the sanitized
-- (credentials/personal values stripped -- see publish_phone_skill's own
-- docstring) skill.yaml-shaped recipe from the doc's section 6, stored as
-- TEXT/JSON-encoded, matching this project's established "payload columns
-- are TEXT, not JSONB" convention (see migration 033's own comment on the
-- same choice for active_tasks.artifacts). success_count/failure_count are
-- the showcase page's "success rate" stat, bumped by
-- db.record_device_skill_outcome every time a replay of this skill
-- actually runs (not by anyone browsing it).
CREATE TABLE IF NOT EXISTS device_skills (
    id SERIAL PRIMARY KEY,
    author_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    author_handle VARCHAR(100),                 -- display name for the showcase page; NULL shows as "a Messa user"
    skill_slug VARCHAR(150) NOT NULL,            -- e.g. 'goodreads_log_book'
    app_name VARCHAR(150) NOT NULL,
    app_package VARCHAR(150) NOT NULL,
    description TEXT,
    recipe_json TEXT NOT NULL,
    is_public BOOLEAN NOT NULL DEFAULT FALSE,
    success_count INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (author_user_id, skill_slug)
);
-- The showcase page's own read pattern: public rows for one app, newest/
-- most-successful first -- see db.list_public_device_skills.
CREATE INDEX IF NOT EXISTS ix_device_skills_public ON device_skills (is_public, app_package, success_count DESC);
