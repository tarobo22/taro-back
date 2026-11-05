-- Existing app_sessions remains the owner identity for v1 and v2.
CREATE TABLE IF NOT EXISTS data_privacy_profiles (
    owner_id uuid PRIMARY KEY REFERENCES app_sessions(id) ON DELETE CASCADE,
    locale text NOT NULL DEFAULT 'ko-KR',
    timezone text NOT NULL DEFAULT 'Asia/Seoul',
    tone text NOT NULL DEFAULT 'warm' CHECK(tone IN ('warm','giga')),
    reduced_motion boolean NOT NULL DEFAULT false,
    voice_mode text NOT NULL DEFAULT 'device' CHECK(voice_mode IN ('device','cloud')),
    state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','deleting','erased')),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS data_privacy_feedback (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    reading_id uuid,
    turn_id uuid,
    rating text NOT NULL CHECK(rating IN ('helpful','unhelpful')),
    reason text NOT NULL CHECK(char_length(reason) BETWEEN 1 AND 100),
    comment text NOT NULL DEFAULT '' CHECK(char_length(comment)<=500),
    idempotency_hash text NOT NULL,
    fingerprint text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK(num_nonnulls(reading_id,turn_id)<=1),
    UNIQUE(owner_id,idempotency_hash)
);
CREATE INDEX IF NOT EXISTS data_privacy_feedback_owner_created ON data_privacy_feedback(owner_id,created_at);

CREATE TABLE IF NOT EXISTS data_privacy_requests (
    id uuid PRIMARY KEY,
    owner_id uuid REFERENCES app_sessions(id) ON DELETE SET NULL,
    kind text NOT NULL CHECK(kind IN ('export','erase')),
    state text NOT NULL CHECK(state IN ('queued','running','completed','failed')),
    idempotency_hash text,
    fingerprint text,
    receipt_hash text NOT NULL UNIQUE,
    export_bytes bytea,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK(kind='export' OR export_bytes IS NULL),
    UNIQUE(owner_id,idempotency_hash)
);
CREATE INDEX IF NOT EXISTS data_privacy_requests_pending ON data_privacy_requests(state,created_at);
CREATE INDEX IF NOT EXISTS data_privacy_requests_owner_created ON data_privacy_requests(owner_id,created_at);
