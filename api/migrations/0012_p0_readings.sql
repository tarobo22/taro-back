CREATE TABLE IF NOT EXISTS p0_readings (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK(kind IN ('daily','three_card')),
    topic text NOT NULL,
    question text NOT NULL DEFAULT '',
    deck_version_id uuid NOT NULL,
    local_day date,
    captured_timezone text NOT NULL,
    state text NOT NULL CHECK(state IN ('draft','selected','generating','ready','failed')),
    version integer NOT NULL DEFAULT 1 CHECK(version>0),
    private_slots jsonb NOT NULL DEFAULT '[]',
    selected_cards jsonb NOT NULL DEFAULT '[]',
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS p0_readings_owner ON p0_readings(owner_id,created_at DESC,id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS p0_readings_daily ON p0_readings(owner_id,local_day,topic) WHERE kind='daily';
CREATE TABLE IF NOT EXISTS p0_daily_draws (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    local_day date NOT NULL,
    topic text NOT NULL,
    card_id integer NOT NULL CHECK(card_id BETWEEN 0 AND 77),
    deck_version_id uuid NOT NULL,
    captured_timezone text NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(owner_id,local_day,topic)
);
CREATE INDEX IF NOT EXISTS p0_daily_draws_expiry ON p0_daily_draws(expires_at);
CREATE TABLE IF NOT EXISTS p0_reading_commands (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    scope text NOT NULL,
    key uuid NOT NULL,
    fingerprint text NOT NULL,
    reading_id uuid NOT NULL,
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(owner_id,scope,key)
);
-- A deleted reading deliberately leaves only its command reference until expiry.
-- No question, slot/card assignment, response or result is copied into this table.
