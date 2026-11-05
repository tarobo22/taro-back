-- Random private per-request material prevents receipt forgery from public IDs
-- and request keys, without storing the receipt or a global signing secret.
-- The material is removed when an erasure receipt is detached.
ALTER TABLE data_privacy_requests ADD COLUMN IF NOT EXISTS receipt_salt bytea;
