-- Existing app_sessions.id remains the personal-data owner. Joining a social
-- account never moves, deletes, or merges another guest's data.
CREATE TABLE IF NOT EXISTS social_accounts (
    owner_id uuid PRIMARY KEY REFERENCES app_sessions(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS social_identities (
    provider text NOT NULL CHECK(provider IN ('google','apple')),
    subject_hash text NOT NULL CHECK(subject_hash ~ '^[0-9a-f]{64}$'),
    owner_id uuid NOT NULL REFERENCES social_accounts(owner_id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(provider,subject_hash),
    UNIQUE(owner_id,provider)
);
CREATE TABLE IF NOT EXISTS social_device_sessions (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    platform text NOT NULL CHECK(platform IN ('web','android','ios')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS social_sessions_owner ON social_device_sessions(owner_id,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS social_auth_flows (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    provider text NOT NULL CHECK(provider IN ('google','apple')),
    state_hash text NOT NULL UNIQUE CHECK(state_hash ~ '^[0-9a-f]{64}$'),
    nonce text, -- SHA-256 of the generated nonce, never its original value.
    provider_verifier text,
    client_challenge text NOT NULL,
    return_uri text NOT NULL,
    platform text NOT NULL CHECK(platform IN ('web','android','ios')),
    intent text NOT NULL CHECK(intent IN ('sign_in','link')),
    state text NOT NULL CHECK(state IN ('pending','processing','ready')),
    expires_at timestamptz NOT NULL,
    ticket_hash text UNIQUE,
    subject_hash text,
    CHECK((state='ready' AND ticket_hash IS NOT NULL AND subject_hash IS NOT NULL
            AND nonce IS NULL AND provider_verifier IS NULL)
        OR (state IN ('pending','processing') AND ticket_hash IS NULL AND subject_hash IS NULL))
);
CREATE INDEX IF NOT EXISTS social_flows_owner ON social_auth_flows(owner_id,expires_at);
CREATE INDEX IF NOT EXISTS social_flows_expiry ON social_auth_flows(expires_at);
