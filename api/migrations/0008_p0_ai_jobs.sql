CREATE TABLE IF NOT EXISTS ai_consent_events (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK(kind IN ('ai_processing','journal_context','cloud_transcription')),
    policy_version text NOT NULL,
    granted boolean NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ai_consents_latest ON ai_consent_events(owner_id,kind,occurred_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS ai_conversations (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    topic text NOT NULL,
    tone text NOT NULL CHECK(tone IN ('warm','giga')),
    state text NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS ai_conversations_owner ON ai_conversations(owner_id,created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS ai_conversation_turns (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    conversation_id uuid NOT NULL REFERENCES ai_conversations(id) ON DELETE CASCADE,
    client_message_id uuid NOT NULL,
    fingerprint text NOT NULL,
    turn_index integer NOT NULL,
    text text NOT NULL,
    answers jsonb NOT NULL,
    input_source text NOT NULL CHECK(input_source IN ('text','choice','voice')),
    state text NOT NULL DEFAULT 'accepted' CHECK(state IN ('accepted','generating','ready','failed','cancelled')),
    assistant_result jsonb,
    version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(conversation_id,turn_index),
    UNIQUE(conversation_id,client_message_id)
);
CREATE TABLE IF NOT EXISTS p0_ai_transcriptions (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','ready','failed','deleted')),
    transcript text,
    audio_path text,
    mime_type text NOT NULL,
    duration_ms integer NOT NULL,
    version integer NOT NULL DEFAULT 1,
    expires_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '1 hour'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS p0_ai_jobs (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    kind text NOT NULL CHECK(kind IN ('tarot','chat','transcription')),
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','cancel_requested','succeeded','failed','cancelled')),
    resource_id uuid NOT NULL,
    resource_version integer NOT NULL,
    conversation_id uuid REFERENCES ai_conversations(id) ON DELETE CASCADE,
    consent_event_id uuid,
    context_consent_id uuid,
    payload jsonb NOT NULL DEFAULT '{}',
    version integer NOT NULL DEFAULT 1,
    lease_id uuid,
    lease_until timestamptz,
    provider_started boolean NOT NULL DEFAULT false,
    error_code text,
    result_url text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
CREATE INDEX IF NOT EXISTS p0_ai_jobs_queue ON p0_ai_jobs(created_at,id) WHERE state='queued';
CREATE INDEX IF NOT EXISTS p0_ai_jobs_owner ON p0_ai_jobs(owner_id,state);
ALTER TABLE p0_ai_jobs ALTER COLUMN consent_event_id DROP NOT NULL;
CREATE TABLE IF NOT EXISTS p0_ai_context_sources (
    job_id uuid NOT NULL REFERENCES p0_ai_jobs(id) ON DELETE CASCADE,
    entry_id uuid NOT NULL,
    entry_version integer NOT NULL,
    PRIMARY KEY(job_id,entry_id)
);
CREATE INDEX IF NOT EXISTS p0_ai_context_entry ON p0_ai_context_sources(entry_id);
CREATE TABLE IF NOT EXISTS p0_ai_idempotency (
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    scope text NOT NULL,
    key uuid NOT NULL,
    fingerprint text NOT NULL,
    resource_id uuid NOT NULL,
    response jsonb NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT (clock_timestamp()+interval '24 hours'),
    PRIMARY KEY(owner_id,scope,key)
);
CREATE TABLE IF NOT EXISTS p0_ai_usage_attempts (
    id uuid PRIMARY KEY,
    owner_id uuid REFERENCES app_sessions(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS p0_ai_usage_day ON p0_ai_usage_attempts(created_at,owner_id);
CREATE TABLE IF NOT EXISTS p0_ai_runs (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES p0_ai_jobs(id) ON DELETE CASCADE,
    model_id text,
    prompt_version text NOT NULL,
    schema_version text NOT NULL,
    request_id text,
    response_id text,
    input_tokens integer,
    output_tokens integer,
    elapsed_ms integer,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
