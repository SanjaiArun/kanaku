\c kanakku

-- A Telegram user can own MULTIPLE profiles: personal, business, a specific
-- bank, a joint account with a partner — each backed by its own Firefly PAT
-- (optionally even a different Firefly III instance via firefly_base_url).
-- Exactly one profile per telegram_user_id is "active" at a time; that's
-- the one new messages get posted to unless the user switches.
CREATE TABLE user_profiles (
    id                 SERIAL PRIMARY KEY,
    telegram_user_id   BIGINT NOT NULL,
    profile_name       TEXT NOT NULL,                 -- 'personal', 'business', 'hdfc-savings'...
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

-- Convenience function n8n calls at insert-time to resolve "whichever
-- profile this user currently has selected" without extra round-trips.
CREATE OR REPLACE FUNCTION get_active_profile(p_telegram_user_id BIGINT)
RETURNS INT AS $$
    SELECT id FROM user_profiles
    WHERE telegram_user_id = p_telegram_user_id AND is_active = TRUE
    LIMIT 1;
$$ LANGUAGE sql STABLE;

-- Every inbound Telegram message becomes a row here.
-- This IS the fallback mechanism: nothing is processed synchronously
-- and lost if a downstream service is unreachable. It's processed
-- from this queue, so a crash/outage just means a delay, not data loss.
CREATE TABLE messages_queue (
    id                 SERIAL PRIMARY KEY,
    telegram_user_id   BIGINT NOT NULL,
    profile_id         INT NOT NULL REFERENCES user_profiles(id),  -- which account this message posts to
    telegram_update_id BIGINT UNIQUE NOT NULL,   -- idempotency key: Telegram may redeliver
    raw_text           TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending',
                       -- pending -> processing -> done
                       --                       -> parse_failed (bad LLM output)
                       --                       -> failed (retries exhausted)
    parsed_json        JSONB,
    retry_count         INT DEFAULT 0,
    last_error         TEXT,
    created_at         TIMESTAMPTZ DEFAULT now(),
    updated_at         TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_queue_status ON messages_queue(status);

CREATE TABLE audit_log (
    id          SERIAL PRIMARY KEY,
    message_id  INT REFERENCES messages_queue(id),
    event       TEXT NOT NULL,      -- e.g. 'llm_parsed', 'firefly_posted', 'telegram_notified', 'retry'
    detail      TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Tracks mid-conversation state per user (clarifications, edit flows, etc.)
CREATE TABLE conversation_state (
    telegram_user_id BIGINT PRIMARY KEY,
    state            TEXT NOT NULL DEFAULT 'idle',
    context          JSONB DEFAULT '{}',
    updated_at       TIMESTAMPTZ DEFAULT now()
);

-- Persists the Telegram getUpdates offset across gateway restarts.
CREATE TABLE telegram_offset (
    id             INT PRIMARY KEY DEFAULT 1,
    last_update_id BIGINT DEFAULT 0
);
INSERT INTO telegram_offset VALUES (1, 0);
