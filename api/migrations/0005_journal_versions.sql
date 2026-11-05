ALTER TABLE journal_entries ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1 CHECK(version>0);
