CREATE TABLE IF NOT EXISTS experience_ai_jobs (
    session_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    fingerprint text NOT NULL,
    lease_id uuid NOT NULL,
    lease_until timestamptz NOT NULL,
    state text NOT NULL CHECK(state IN ('pending','failed','completed')),
    PRIMARY KEY(session_id,request_id)
);
CREATE TABLE IF NOT EXISTS ai_usage_attempts (
    id uuid PRIMARY KEY,
    session_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_usage_day ON ai_usage_attempts(created_at,session_id);
-- Preserve usage of pre-migration successful calls without counting retries twice.
INSERT INTO ai_usage_attempts(id,session_id,request_id,created_at)
SELECT id,session_id,id,created_at FROM counsel_sessions WHERE advice->>'source'='openai'
ON CONFLICT(id) DO NOTHING;
