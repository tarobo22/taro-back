from psycopg.rows import dict_row

from api.domain.identity import DeviceSession


class PostgresIdentityRepository:
    def __init__(self, connection, initialize_profile, owner_guard):
        self.connection, self.initialize_profile, self.owner_guard = connection, initialize_profile, owner_guard

    def create(self, owner, token_hash, expires_at, platform, locale, timezone):
        self.connection.execute(
            'INSERT INTO app_sessions(id,token_hash,expires_at,platform) VALUES(%s,%s,%s,%s)',
            (owner, token_hash, expires_at, platform))
        self.initialize_profile(self.connection, owner, locale, timezone)

    def lock(self, owner):
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                                (f'yeoun:owner:{owner}',))

    def _session(self, row):
        if row is None or not self.owner_guard(self.connection, row['id']):
            return None
        return DeviceSession(row['id'], row['expires_at'])

    def find(self, token_hash):
        row = self.connection.execute('SELECT id,expires_at FROM app_sessions WHERE token_hash=%s',
                                      (token_hash,)).fetchone()
        return self._session(row)

    def get(self, owner):
        return self._session(self.connection.execute('SELECT id,expires_at FROM app_sessions WHERE id=%s',
                                                     (owner,)).fetchone())

    def renew(self, owner, expires_at):
        self.connection.execute('UPDATE app_sessions SET expires_at=%s,last_seen_at=now() WHERE id=%s',
                                (expires_at, owner))


class PostgresIdentityUnitOfWork:
    def __init__(self, pool, initialize_profile, owner_guard):
        self.pool, self.initialize_profile, self.owner_guard = pool, initialize_profile, owner_guard

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.sessions = PostgresIdentityRepository(self.connection, self.initialize_profile, self.owner_guard)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
