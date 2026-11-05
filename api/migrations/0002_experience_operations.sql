CREATE TABLE IF NOT EXISTS experience_operations(
  session_id uuid NOT NULL REFERENCES app_sessions(id),
  kind text NOT NULL,
  request_id uuid NOT NULL,
  fingerprint text NOT NULL,
  result jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(session_id,kind,request_id)
);
ALTER TABLE counsel_sessions ADD COLUMN IF NOT EXISTS parent_id uuid REFERENCES counsel_sessions(id);
CREATE INDEX IF NOT EXISTS counsel_owner_created ON counsel_sessions(session_id,created_at DESC);
CREATE INDEX IF NOT EXISTS readings_owner_created ON tarot_readings(session_id,created_at DESC);
