from psycopg.rows import dict_row
from api.adapters.data_privacy_postgres import lock_owner, require_active_owner


class PostgresBillingRepository:
    def __init__(self, connection):self.connection = connection
    def lock(self, owner, token_hash=None):
        lock_owner(self.connection, owner)
        if token_hash:self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('yeoun:receipt:'+token_hash,))
    def active(self, owner):require_active_owner(self.connection, owner)
    def get(self, token_hash):return self.connection.execute('SELECT * FROM billing_receipts WHERE token_hash=%s', (token_hash,)).fetchone()
    def count(self, owner):return self.connection.execute('SELECT count(*) AS n FROM billing_receipts WHERE owner_id=%s', (owner,)).fetchone()['n']
    def list(self, owner):return self.connection.execute('SELECT * FROM billing_receipts WHERE owner_id=%s ORDER BY verified_at,token_hash', (owner,)).fetchall()
    def save(self, values):
        self.connection.execute('''INSERT INTO billing_receipts(token_hash,owner_id,product_id,product_kind,entitlement_key,
            token_ciphertext,state,expires_at,acknowledged,verified_at) VALUES(%(token_hash)s,%(owner_id)s,%(product_id)s,
            %(product_kind)s,%(entitlement_key)s,%(token_ciphertext)s,%(state)s,%(expires_at)s,%(acknowledged)s,%(verified_at)s)
            ON CONFLICT(token_hash) DO UPDATE SET token_ciphertext=EXCLUDED.token_ciphertext,state=EXCLUDED.state,
            expires_at=EXCLUDED.expires_at,acknowledged=EXCLUDED.acknowledged,verified_at=EXCLUDED.verified_at,
            verification_version=billing_receipts.verification_version+1''', values)
    def acknowledge(self, owner, token_hash):
        self.connection.execute('UPDATE billing_receipts SET acknowledged=true WHERE owner_id=%s AND token_hash=%s', (owner, token_hash))
    def revoke_linked(self, owner, token_hash):
        self.connection.execute("UPDATE billing_receipts SET state='revoked',verification_version=verification_version+1 WHERE owner_id=%s AND token_hash=%s", (owner, token_hash))
    def due(self, before, limit):
        return self.connection.execute('''SELECT r.* FROM billing_receipts r JOIN data_privacy_profiles p ON p.owner_id=r.owner_id
            WHERE p.state='active' AND (r.verified_at<=%s OR (r.state='active' AND NOT r.acknowledged))
            ORDER BY r.verified_at,r.token_hash LIMIT %s''', (before, limit)).fetchall()
    def erase(self, owner):self.connection.execute('DELETE FROM billing_receipts WHERE owner_id=%s', (owner,))


class PostgresBillingUnitOfWork:
    def __init__(self, pool):self.pool = pool
    def repository_for(self, connection):return PostgresBillingRepository(connection)
    def __enter__(self):
        self.connection = self.pool.getconn();self.connection.row_factory = dict_row
        self.receipts = self.repository_for(self.connection);return self
    def commit(self):self.connection.commit()
    def __exit__(self, *exc):
        try:self.connection.rollback()
        finally:self.pool.putconn(self.connection)
