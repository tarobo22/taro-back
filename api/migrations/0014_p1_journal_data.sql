-- Composite identity enforces that a favorite and its v2 source share an owner.
CREATE UNIQUE INDEX IF NOT EXISTS p0_readings_owner_identity ON p0_readings(owner_id,id);

CREATE TABLE IF NOT EXISTS favorites (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    reading_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(owner_id,reading_id),
    FOREIGN KEY(owner_id,reading_id) REFERENCES p0_readings(owner_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS favorites_owner_created ON favorites(owner_id,created_at DESC,reading_id DESC);

-- Minimal original response metadata, no reading text, result, or JSON snapshots.
-- A source deletion nulls the reference but retains a spent key and fingerprint;
-- replay cannot recreate the source or favorite, and another payload conflicts.
-- All metadata is removed by the owner's privacy-erasure callback.
CREATE TABLE IF NOT EXISTS p1_favorite_create_requests (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    key_hash text NOT NULL,
    fingerprint text NOT NULL,
    reading_id uuid REFERENCES p0_readings(id) ON DELETE SET NULL,
    favorite_created_at timestamptz NOT NULL,
    PRIMARY KEY(owner_id,key_hash)
);
