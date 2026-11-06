CREATE TABLE IF NOT EXISTS story_runs (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    run_sequence bigint GENERATED ALWAYS AS IDENTITY,
    episode_id text NOT NULL,
    episode_version text NOT NULL,
    state text NOT NULL CHECK(state IN ('active','completed','restarted')),
    version integer NOT NULL DEFAULT 1 CHECK(version >= 1),
    current_node_id text,
    ending_id text,
    history jsonb NOT NULL DEFAULT '[]'::jsonb CHECK(jsonb_typeof(history)='array' AND jsonb_array_length(history)<=5),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    UNIQUE(owner_id,id),
    CHECK((state='active' AND current_node_id IS NOT NULL AND ending_id IS NULL AND completed_at IS NULL)
       OR (state='completed' AND current_node_id IS NULL AND ending_id IS NOT NULL AND completed_at IS NOT NULL)
       OR (state='restarted' AND current_node_id IS NULL AND ending_id IS NULL AND completed_at IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS story_one_active_episode ON story_runs(owner_id,episode_id) WHERE state='active';
CREATE INDEX IF NOT EXISTS story_latest_progress ON story_runs(owner_id,episode_id,run_sequence DESC);

-- Request fingerprints contain no story prose or private journal text. Owner erasure removes them.
CREATE TABLE IF NOT EXISTS story_requests (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    key_hash text NOT NULL CHECK(key_hash ~ '^[a-f0-9]{64}$'),
    fingerprint text NOT NULL CHECK(fingerprint ~ '^[a-f0-9]{64}$'),
    result_run_id uuid NOT NULL,
    PRIMARY KEY(owner_id,key_hash),
    FOREIGN KEY(owner_id,result_run_id) REFERENCES story_runs(owner_id,id) ON DELETE CASCADE
);
