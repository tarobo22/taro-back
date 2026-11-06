from psycopg.rows import dict_row

from api.adapters.data_privacy_postgres import lock_owner, require_active_owner


class PostgresSocialRepository:
    def __init__(self, connection):
        self.connection = connection

    def lock_owner(self, owner):
        lock_owner(self.connection, owner)

    def active(self, owner):
        require_active_owner(self.connection, owner)

    def lock_identity(self, provider, subject_hash):
        # Account exchanges are infrequent short transactions. A common lock
        # precedes all owner locks, preventing cross-account link lock cycles.
        self.connection.execute("SELECT pg_advisory_xact_lock(hashtextextended('yeoun:social-account-admission',0))")

    def purge(self, now):
        self.connection.execute('''DELETE FROM social_auth_flows WHERE id IN
            (SELECT id FROM social_auth_flows WHERE expires_at<=%s ORDER BY expires_at,id
             LIMIT 500 FOR UPDATE SKIP LOCKED)''', (now,))

    def flow_count(self, owner, now):
        return self.connection.execute('SELECT count(*) AS n FROM social_auth_flows WHERE owner_id=%s AND expires_at>%s',
                                       (owner, now)).fetchone()['n']

    def add_flow(self, row):
        self.connection.execute('''INSERT INTO social_auth_flows
            (id,owner_id,provider,state_hash,nonce,provider_verifier,client_challenge,return_uri,platform,intent,state,expires_at)
            VALUES(%(id)s,%(owner_id)s,%(provider)s,%(state_hash)s,%(nonce)s,%(provider_verifier)s,
                   %(client_challenge)s,%(return_uri)s,%(platform)s,%(intent)s,%(state)s,%(expires_at)s)''', row)

    def flow(self, state_hash):
        return self.connection.execute('SELECT * FROM social_auth_flows WHERE state_hash=%s', (state_hash,)).fetchone()

    def claim_flow(self, state_hash, provider, now):
        return self.connection.execute("""UPDATE social_auth_flows SET state='processing'
            WHERE state_hash=%s AND provider=%s AND state='pending' AND expires_at>%s RETURNING *""",
            (state_hash, provider, now)).fetchone()

    def complete_flow(self, state_hash, subject_hash, ticket_hash, expires_at):
        return self.connection.execute("""UPDATE social_auth_flows SET state='ready',subject_hash=%s,ticket_hash=%s,
            nonce=NULL,provider_verifier=NULL,expires_at=%s WHERE state_hash=%s AND state='processing'""",
            (subject_hash, ticket_hash, expires_at, state_hash)).rowcount == 1

    def discard_flow(self, state_hash):
        self.connection.execute('DELETE FROM social_auth_flows WHERE state_hash=%s', (state_hash,))

    def ticket(self, owner, ticket_hash, now):
        return self.connection.execute("""SELECT * FROM social_auth_flows WHERE owner_id=%s
            AND ticket_hash=%s AND state='ready' AND expires_at>%s""", (owner, ticket_hash, now)).fetchone()

    def identity_owner(self, provider, subject_hash):
        row = self.connection.execute('SELECT owner_id FROM social_identities WHERE provider=%s AND subject_hash=%s',
                                       (provider, subject_hash)).fetchone()
        return row['owner_id'] if row else None

    def member(self, owner):
        return bool(self.connection.execute('SELECT 1 FROM social_accounts WHERE owner_id=%s', (owner,)).fetchone())

    def has_provider(self, owner, provider):
        return bool(self.connection.execute('SELECT 1 FROM social_identities WHERE owner_id=%s AND provider=%s', (owner, provider)).fetchone())

    def link_identity(self, owner, provider, subject_hash):
        self.connection.execute('INSERT INTO social_accounts(owner_id) VALUES(%s) ON CONFLICT DO NOTHING', (owner,))
        self.connection.execute('INSERT INTO social_identities(provider,subject_hash,owner_id) VALUES(%s,%s,%s)', (provider, subject_hash, owner))

    def providers(self, owner):
        return [row['provider'] for row in self.connection.execute('SELECT provider FROM social_identities WHERE owner_id=%s ORDER BY provider', (owner,))]

    def capture_legacy_session(self, owner):
        # The guest token becomes a tracked member session. Its later revocation
        # is recognized before the legacy authenticator can accept that token.
        self.connection.execute('''INSERT INTO social_device_sessions(id,owner_id,token_hash,platform,created_at,expires_at)
            SELECT id,id,token_hash,platform,created_at,expires_at FROM app_sessions WHERE id=%s ON CONFLICT DO NOTHING''', (owner,))

    def add_session(self, row):
        self.connection.execute('''INSERT INTO social_device_sessions(id,owner_id,token_hash,platform,created_at,expires_at)
            VALUES(%(id)s,%(owner_id)s,%(token_hash)s,%(platform)s,%(created_at)s,%(expires_at)s)''', row)

    def session_by_hash(self, token_hash):
        return self.connection.execute('SELECT * FROM social_device_sessions WHERE token_hash=%s', (token_hash,)).fetchone()

    def renew(self, session_id, expires_at):
        self.connection.execute('UPDATE social_device_sessions SET expires_at=%s WHERE id=%s AND revoked_at IS NULL', (expires_at, session_id))

    def list_sessions(self, owner):
        return self.connection.execute('SELECT * FROM social_device_sessions WHERE owner_id=%s ORDER BY created_at DESC,id DESC', (owner,)).fetchall()

    def revoke(self, owner, session_id, now):
        return self.connection.execute('UPDATE social_device_sessions SET revoked_at=COALESCE(revoked_at,%s) WHERE owner_id=%s AND id=%s',
                                       (now, owner, session_id)).rowcount == 1

    def revoke_all(self, owner, now):
        self.connection.execute('UPDATE social_device_sessions SET revoked_at=COALESCE(revoked_at,%s) WHERE owner_id=%s', (now, owner))
        self.connection.execute('UPDATE app_sessions SET expires_at=LEAST(expires_at,%s) WHERE id=%s', (now, owner))

    def export_owner(self, owner):
        identities = self.connection.execute('SELECT provider,created_at FROM social_identities WHERE owner_id=%s ORDER BY provider', (owner,)).fetchall()
        sessions = self.connection.execute('''SELECT id,platform,created_at,expires_at,revoked_at FROM social_device_sessions
            WHERE owner_id=%s ORDER BY created_at,id''', (owner,)).fetchall()
        return {'social_accounts': identities, 'social_sessions': sessions}

    def erase_owner(self, owner):
        for table in ('social_auth_flows', 'social_device_sessions', 'social_accounts'):
            self.connection.execute(f'DELETE FROM {table} WHERE owner_id=%s', (owner,))


class PostgresSocialUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def repository_for(self, transaction):
        return PostgresSocialRepository(transaction)

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.social = self.repository_for(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
