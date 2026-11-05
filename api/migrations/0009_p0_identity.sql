ALTER TABLE app_sessions ADD COLUMN IF NOT EXISTS expires_at timestamptz NOT NULL DEFAULT (now()+interval '180 days');
ALTER TABLE app_sessions ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE app_sessions ADD COLUMN IF NOT EXISTS platform text NOT NULL DEFAULT 'web' CHECK(platform IN ('web','android','ios'));
