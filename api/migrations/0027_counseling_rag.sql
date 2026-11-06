CREATE TABLE IF NOT EXISTS counseling_sessions (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    create_key uuid NOT NULL,
    topic text NOT NULL,
    state jsonb NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK(version>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS counseling_create_key ON counseling_sessions(owner_id,create_key);
CREATE UNIQUE INDEX IF NOT EXISTS counseling_sessions_id_owner ON counseling_sessions(id,owner_id);
CREATE INDEX IF NOT EXISTS counseling_sessions_owner ON counseling_sessions(owner_id,updated_at DESC,id);
CREATE TABLE IF NOT EXISTS counseling_commands (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    session_id uuid NOT NULL REFERENCES counseling_sessions(id) ON DELETE CASCADE,
    request_key uuid NOT NULL,
    fingerprint text NOT NULL,
    kind text NOT NULL CHECK(kind IN ('message','confirm')),
    payload jsonb NOT NULL,
    consent_event_id uuid NOT NULL REFERENCES ai_consent_events(id),
    state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','succeeded','failed')),
    error_code text,
    timings jsonb NOT NULL DEFAULT '{}',
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    UNIQUE(owner_id,request_key)
);
CREATE INDEX IF NOT EXISTS counseling_commands_queue ON counseling_commands(state,created_at);
CREATE TABLE IF NOT EXISTS personal_glossary (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    normalized_term text NOT NULL,
    term text NOT NULL CHECK(length(term) BETWEEN 1 AND 60),
    definition text NOT NULL CHECK(length(definition) BETWEEN 1 AND 400),
    evidence_quote text NOT NULL DEFAULT '',
    source_message_id uuid,
    version integer NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(owner_id,normalized_term)
);

-- Migrations use a schema-owner connection. Requests switch to a non-login role.
DO $$ BEGIN
    IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='yeoun_counseling_app') THEN
        CREATE ROLE yeoun_counseling_app NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
    IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='yeoun_counseling_app' AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'Counseling role must not bypass row security';
    END IF;
    EXECUTE format('GRANT yeoun_counseling_app TO %I',current_user);
END $$;
GRANT USAGE ON SCHEMA public TO yeoun_counseling_app;
GRANT SELECT,INSERT,UPDATE,DELETE ON counseling_sessions,counseling_commands,personal_glossary TO yeoun_counseling_app;
GRANT SELECT ON app_sessions,data_privacy_profiles,ai_consent_events,p0_ai_usage_attempts,ai_usage_attempts TO yeoun_counseling_app;
GRANT INSERT ON p0_ai_usage_attempts TO yeoun_counseling_app;
ALTER TABLE counseling_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE counseling_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE counseling_commands ENABLE ROW LEVEL SECURITY;
ALTER TABLE counseling_commands FORCE ROW LEVEL SECURITY;
ALTER TABLE personal_glossary ENABLE ROW LEVEL SECURITY;
ALTER TABLE personal_glossary FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS counseling_owner ON counseling_sessions;
CREATE POLICY counseling_owner ON counseling_sessions
    USING(owner_id=nullif(current_setting('yeoun.owner',true),'')::uuid)
    WITH CHECK(owner_id=nullif(current_setting('yeoun.owner',true),'')::uuid);
DROP POLICY IF EXISTS counseling_owner ON counseling_commands;
CREATE POLICY counseling_owner ON counseling_commands
    USING(owner_id=nullif(current_setting('yeoun.owner',true),'')::uuid)
    WITH CHECK(owner_id=nullif(current_setting('yeoun.owner',true),'')::uuid);
DROP POLICY IF EXISTS counseling_owner ON personal_glossary;
CREATE POLICY counseling_owner ON personal_glossary
    USING(owner_id=nullif(current_setting('yeoun.owner',true),'')::uuid)
    WITH CHECK(owner_id=nullif(current_setting('yeoun.owner',true),'')::uuid);

-- The dispatcher can lease work, but returns identifiers, never private payloads.
CREATE OR REPLACE FUNCTION public.claim_counseling_command()
RETURNS TABLE(id uuid,owner_id uuid,session_id uuid)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE selected uuid;
BEGIN
    UPDATE public.counseling_commands SET state='failed',error_code='UNKNOWN_PROVIDER_OUTCOME',finished_at=clock_timestamp()
        WHERE state='running' AND lease_until<clock_timestamp();
    SELECT c.id INTO selected FROM public.counseling_commands c WHERE c.state='queued'
        ORDER BY c.created_at,c.id FOR UPDATE SKIP LOCKED LIMIT 1;
    IF selected IS NULL THEN RETURN; END IF;
    RETURN QUERY UPDATE public.counseling_commands c SET state='running',lease_until=clock_timestamp()+interval '5 minutes'
        WHERE c.id=selected RETURNING c.id,c.owner_id,c.session_id;
END $$;
REVOKE ALL ON FUNCTION public.claim_counseling_command() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.claim_counseling_command() TO yeoun_counseling_app;
