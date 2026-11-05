CREATE TABLE IF NOT EXISTS journal_create_requests (
    session_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    fingerprint text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, request_id)
);
