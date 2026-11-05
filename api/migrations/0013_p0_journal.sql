-- v2 exposes the existing journal_entries rows and CAS versions. No diary
-- body/result snapshots are copied into source or idempotency metadata.
CREATE TABLE IF NOT EXISTS journal_v2_sources (
    entry_id uuid PRIMARY KEY REFERENCES journal_entries(id) ON DELETE CASCADE,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    source_reading_id uuid,
    source_turn_id uuid,
    CHECK(num_nonnulls(source_reading_id,source_turn_id)=1)
);
CREATE INDEX IF NOT EXISTS journal_v2_sources_owner ON journal_v2_sources(owner_id);
CREATE TABLE IF NOT EXISTS journal_v2_create_requests (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    key_hash text NOT NULL,
    fingerprint text,
    entry_id uuid REFERENCES journal_entries(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(owner_id,key_hash)
);
CREATE TABLE IF NOT EXISTS journal_v2_deleted_entries (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    entry_id uuid NOT NULL,
    deleted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(owner_id,entry_id)
);
CREATE INDEX IF NOT EXISTS journal_v2_owner_created ON journal_entries(session_id,created_at DESC,id DESC);
