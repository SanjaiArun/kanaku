\c kanakku

-- A Telegram user can own at most TWO profiles: 'personal' and
-- 'business' — each backed by its own Firefly PAT (a separate,
-- automatically-provisioned Firefly III user; see
-- gateway/firefly_admin.py). Exactly one profile per telegram_user_id
-- is "active" at a time; that's the one new messages get posted to
-- unless the user switches.
CREATE TABLE user_profiles (
    id                 SERIAL PRIMARY KEY,
    telegram_user_id   BIGINT NOT NULL,
    profile_name       TEXT NOT NULL CHECK (profile_name IN ('personal', 'business')),
    display_name       TEXT,
    firefly_pat        TEXT NOT NULL,
    firefly_base_url   TEXT NOT NULL DEFAULT 'http://firefly_iii:8080',
    default_currency   TEXT DEFAULT 'INR',
    is_active          BOOLEAN NOT NULL DEFAULT FALSE, -- the currently-selected profile for this user
    created_at         TIMESTAMPTZ DEFAULT now(),
    UNIQUE (telegram_user_id, profile_name)
);

-- Enforce "at most one active profile per user" at the DB level, not just in app code.
CREATE UNIQUE INDEX one_active_profile_per_user
    ON user_profiles (telegram_user_id) WHERE is_active;

-- Persists the Telegram getUpdates offset across gateway restarts.
CREATE TABLE telegram_offset (
    id             INT PRIMARY KEY DEFAULT 1,
    last_update_id BIGINT DEFAULT 0
);
INSERT INTO telegram_offset VALUES (1, 0);

-- Conversation memory (chat history, pending confirmations, clarification
-- state) is no longer tracked by hand here — the LangGraph agent's
-- Postgres checkpointer owns it, and creates its own tables
-- (checkpoints, checkpoint_blobs, checkpoint_writes, ...) in this
-- database on gateway startup.
