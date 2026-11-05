ALTER TABLE daily_readings ADD COLUMN IF NOT EXISTS id uuid NOT NULL DEFAULT gen_random_uuid();
ALTER TABLE daily_readings ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
