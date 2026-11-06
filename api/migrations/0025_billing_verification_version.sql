-- CAS for store verification performed outside a database transaction.
-- A monotonic counter detects concurrent updates even if verified_at is equal.
ALTER TABLE billing_receipts
    ADD COLUMN IF NOT EXISTS verification_version bigint NOT NULL DEFAULT 1
    CHECK (verification_version >= 1);
