-- No operator roles or publications are seeded. Grants require the explicit
-- local CLI and an existing social member; user sessions cannot call admin APIs.
CREATE TABLE IF NOT EXISTS admin_roles (
    owner_id uuid PRIMARY KEY REFERENCES app_sessions(id) ON DELETE CASCADE,
    role text NOT NULL CHECK(role IN ('viewer','publisher')),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS admin_sessions (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE CHECK(token_hash ~ '^[0-9a-f]{64}$'),
    source_session_id uuid NOT NULL REFERENCES social_device_sessions(id) ON DELETE CASCADE,
    role text NOT NULL CHECK(role IN ('viewer','publisher')),
    expires_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_sessions_owner ON admin_sessions(owner_id);
CREATE INDEX IF NOT EXISTS admin_sessions_expiry ON admin_sessions(expires_at);
CREATE TABLE IF NOT EXISTS admin_catalog_versions (
    version_id uuid PRIMARY KEY,
    snapshot jsonb NOT NULL,
    snapshot_hash text NOT NULL CHECK(snapshot_hash ~ '^[0-9a-f]{64}$'),
    rights_status text NOT NULL CHECK(rights_status IN ('unreviewed','approved','rejected')),
    review_note text NOT NULL,
    review_version integer NOT NULL CHECK(review_version >= 0)
);
CREATE TABLE IF NOT EXISTS admin_catalog_publications (
    id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES admin_catalog_versions(version_id),
    code text NOT NULL,
    action text NOT NULL CHECK(action IN ('publish','rollback')),
    previous_id uuid REFERENCES admin_catalog_publications(id),
    snapshot jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_publications_code ON admin_catalog_publications(code,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS admin_catalog_heads (
    code text PRIMARY KEY,
    publication_id uuid REFERENCES admin_catalog_publications(id),
    version integer NOT NULL CHECK(version > 0)
);
CREATE TABLE IF NOT EXISTS admin_feedback_triage (
    feedback_id uuid PRIMARY KEY REFERENCES data_privacy_feedback(id) ON DELETE CASCADE,
    status text NOT NULL CHECK(status IN ('new','reviewing','resolved','dismissed')),
    version integer NOT NULL CHECK(version > 0)
);
CREATE TABLE IF NOT EXISTS admin_ota_policy (
    singleton integer PRIMARY KEY CHECK(singleton=1),
    channel text NOT NULL CHECK(channel ~ '^[A-Za-z0-9_-]{1,40}$'),
    min_native_version text NOT NULL,
    updates_enabled boolean NOT NULL,
    version integer NOT NULL CHECK(version > 0)
);
CREATE TABLE IF NOT EXISTS admin_audit (
    id uuid PRIMARY KEY,
    actor_owner_id uuid REFERENCES app_sessions(id) ON DELETE SET NULL,
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_audit_created ON admin_audit(created_at DESC,id DESC);
