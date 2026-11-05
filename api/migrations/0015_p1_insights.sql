-- Original source references deliberately survive journal/consent deletion.
-- No journal body, mood or provider prompt is copied into these tables.
ALTER TABLE p0_ai_jobs DROP CONSTRAINT IF EXISTS p0_ai_jobs_kind_check;
ALTER TABLE p0_ai_jobs ADD CONSTRAINT p0_ai_jobs_kind_check CHECK(kind IN ('tarot','chat','transcription','insight'));

CREATE TABLE IF NOT EXISTS journal_insights (
    id uuid PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    range_start date NOT NULL,
    range_end date NOT NULL CHECK(range_end>=range_start),
    state text NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','generating','ready','failed','cancelled','invalidated')),
    based_on_count integer NOT NULL CHECK(based_on_count BETWEEN 1 AND 10),
    result jsonb,
    consent_event_id uuid NOT NULL,
    context_consent_id uuid NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK(version>0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK(state='ready' OR result IS NULL)
);
CREATE INDEX IF NOT EXISTS journal_insights_owner ON journal_insights(owner_id,created_at,id);
CREATE TABLE IF NOT EXISTS p1_journal_insight_sources (
    insight_id uuid NOT NULL REFERENCES journal_insights(id) ON DELETE CASCADE,
    entry_id uuid NOT NULL,
    entry_version integer NOT NULL CHECK(entry_version>0),
    position integer NOT NULL CHECK(position BETWEEN 1 AND 10),
    PRIMARY KEY(insight_id,entry_id),
    UNIQUE(insight_id,position)
);
CREATE INDEX IF NOT EXISTS p1_insight_source_entry ON p1_journal_insight_sources(entry_id);
