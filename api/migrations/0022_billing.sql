CREATE TABLE IF NOT EXISTS billing_receipts(
    token_hash text PRIMARY KEY CHECK(length(token_hash)=64),
    owner_id uuid NOT NULL REFERENCES app_sessions(id) ON DELETE CASCADE,
    product_id text NOT NULL,
    product_kind text NOT NULL CHECK(product_kind IN ('inapp','subs')),
    entitlement_key text NOT NULL,
    token_ciphertext text NOT NULL,
    state text NOT NULL CHECK(state IN ('active','pending','expired','revoked')),
    expires_at timestamptz,
    acknowledged boolean NOT NULL,
    verified_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS billing_receipts_owner ON billing_receipts(owner_id);
CREATE INDEX IF NOT EXISTS billing_receipts_refresh ON billing_receipts(verified_at);
