import json
from pathlib import Path

from psycopg import sql
from psycopg.rows import dict_row

from api.domain.data_privacy import DataNotFound, DataPolicy, OwnerDeleting


def initialize_profile(connection, owner, locale='ko-KR', timezone='Asia/Seoul'):
    """Initialize in the caller's session-insert transaction; no implicit commit."""
    policy = DataPolicy.from_mapping(json.loads(
        (Path(__file__).parents[2] / 'shared/data-policy.json').read_text(encoding='utf-8')))
    values = policy.preference_patch({'locale': locale, 'timezone': timezone})
    lock_owner(connection, owner)
    require_active_owner(connection, owner)
    connection.execute('''INSERT INTO data_privacy_profiles(owner_id,locale,timezone)
        VALUES(%s,%s,%s) ON CONFLICT(owner_id) DO NOTHING''', (owner, values['locale'], values['timezone']))


def lock_owner(connection, owner):
    connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', (f'yeoun:owner:{owner}',))


def require_active_owner(connection, owner):
    """Call while holding lock_owner before state changes or sensitive final commits."""
    row = connection.execute('''SELECT p.state FROM app_sessions s
        LEFT JOIN data_privacy_profiles p ON p.owner_id=s.id WHERE s.id=%s''', (owner,)).fetchone()
    if row is None or row['state'] == 'erased':
        raise DataNotFound('owner_not_found')
    if row['state'] == 'deleting':
        raise OwnerDeleting('owner_deleting')


def owner_guard(connection, owner):
    """Boolean composition seam for jobs and authentication; absence of profile is active."""
    row = connection.execute('''SELECT COALESCE(p.state,'active')='active' AS active FROM app_sessions s
        LEFT JOIN data_privacy_profiles p ON p.owner_id=s.id WHERE s.id=%s''', (owner,)).fetchone()
    return bool(row and row['active'])


class PostgresDataPrivacyRepository:
    def __init__(self, connection):
        self.connection = connection

    def lock(self, owner):
        lock_owner(self.connection, owner)

    def active(self, owner):
        require_active_owner(self.connection, owner)

    def profile(self, owner, defaults):
        self.connection.execute('''INSERT INTO data_privacy_profiles(owner_id,locale,timezone)
            VALUES(%s,%s,%s) ON CONFLICT(owner_id) DO NOTHING''',
            (owner, defaults.default_locale, defaults.default_timezone))
        return self.connection.execute('SELECT * FROM data_privacy_profiles WHERE owner_id=%s', (owner,)).fetchone()

    def update_profile(self, owner, values):
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in values)
        self.connection.execute(sql.SQL('UPDATE data_privacy_profiles SET {},updated_at=now() WHERE owner_id=%s').format(assignments),
                                (*values.values(), owner))

    def feedback_operation(self, owner, key_hash):
        return self.connection.execute('''SELECT id,fingerprint,created_at FROM data_privacy_feedback
            WHERE owner_id=%s AND idempotency_hash=%s''', (owner, key_hash)).fetchone()

    def feedback_count(self, owner, day_start):
        return self.connection.execute('''SELECT count(*) AS total FROM data_privacy_feedback
            WHERE owner_id=%s AND created_at>=%s''', (owner, day_start)).fetchone()['total']

    def legacy_target_exists(self, owner, kind, target):
        if kind != 'reading':
            return False
        return bool(self.connection.execute('''SELECT 1 FROM tarot_readings WHERE session_id=%s AND id=%s
            UNION ALL SELECT 1 FROM daily_readings WHERE session_id=%s AND id=%s''',
            (owner, target, owner, target)).fetchone())

    def add_feedback(self, owner, feedback_id, values, key_hash, fingerprint):
        return self.connection.execute('''INSERT INTO data_privacy_feedback
            (id,owner_id,reading_id,turn_id,rating,reason,comment,idempotency_hash,fingerprint)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id,created_at''',
            (feedback_id, owner, values.get('reading_id'), values.get('turn_id'), values['rating'],
             values['reason'], values['comment'], key_hash, fingerprint)).fetchone()


class PostgresDataPrivacyUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.data = PostgresDataPrivacyRepository(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
