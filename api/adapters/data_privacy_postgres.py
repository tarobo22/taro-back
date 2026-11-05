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

    def request_operation(self, owner, key_hash):
        return self.connection.execute('''SELECT * FROM data_privacy_requests
            WHERE owner_id=%s AND idempotency_hash=%s''', (owner, key_hash)).fetchone()

    def request_count(self, owner, day_start):
        return self.connection.execute('''SELECT count(*) AS total FROM data_privacy_requests
            WHERE owner_id=%s AND created_at>=%s''', (owner, day_start)).fetchone()['total']

    def add_request(self, request_id, owner, kind, key_hash, fingerprint, receipt_hash, expires_at, receipt_salt):
        return self.connection.execute('''INSERT INTO data_privacy_requests
            (id,owner_id,kind,state,idempotency_hash,fingerprint,receipt_hash,expires_at,receipt_salt)
            VALUES(%s,%s,%s,'queued',%s,%s,%s,%s,%s) RETURNING *''',
            (request_id, owner, kind, key_hash, fingerprint, receipt_hash, expires_at, receipt_salt)).fetchone()

    def request(self, request_id, *, for_update=False):
        suffix = ' FOR UPDATE' if for_update else ''
        return self.connection.execute('SELECT * FROM data_privacy_requests WHERE id=%s' + suffix,
                                       (request_id,)).fetchone()

    def set_deleting(self, owner):
        self.connection.execute("UPDATE data_privacy_profiles SET state='deleting',updated_at=now() WHERE owner_id=%s", (owner,))

    def mark_running(self, request_id):
        self.connection.execute("UPDATE data_privacy_requests SET state='running',updated_at=now() WHERE id=%s", (request_id,))

    def mark_failed(self, request_id):
        self.connection.execute("""UPDATE data_privacy_requests SET state='failed',export_bytes=NULL,updated_at=now()
            WHERE id=%s AND state<>'completed'""", (request_id,))

    def complete_export(self, request_id, archive, expires_at):
        self.connection.execute("""UPDATE data_privacy_requests SET state='completed',export_bytes=%s,
            expires_at=%s,updated_at=now() WHERE id=%s""", (archive, expires_at, request_id))

    def expire_export(self, request_id):
        self.connection.execute('UPDATE data_privacy_requests SET export_bytes=NULL WHERE id=%s', (request_id,))

    def export_legacy(self, owner):
        collections = {}
        fields = {
            'journal_entries': 'id,day,mood,body,created_at,updated_at,version',
            'tarot_readings': 'id,topic,question,card_ids,result,created_at',
            'daily_readings': 'id,day,topic,result,created_at',
            'counsel_sessions': 'id,topic,question,answers,tone,advice,parent_id,created_at',
        }
        for table, columns in fields.items():
            name = 'readings' if table == 'tarot_readings' else table
            collections[name] = self.connection.execute(sql.SQL('SELECT {} FROM {} WHERE session_id=%s ORDER BY created_at,id').format(
                sql.SQL(columns), sql.Identifier(table)), (owner,)).fetchall()
        collections['feedback'] = self.connection.execute('''SELECT id,reading_id,turn_id,rating,reason,comment,created_at
            FROM data_privacy_feedback WHERE owner_id=%s ORDER BY created_at,id''', (owner,)).fetchall()
        return collections

    def erase_legacy(self, owner):
        # These are v1 personal records and retry snapshots. Usage counters are retained.
        for table in ('experience_ai_jobs', 'experience_operations', 'journal_create_requests',
                      'journal_entries', 'tarot_readings', 'daily_readings', 'counsel_sessions'):
            self.connection.execute(sql.SQL('DELETE FROM {} WHERE session_id=%s').format(sql.Identifier(table)), (owner,))
        self.connection.execute('DELETE FROM data_privacy_feedback WHERE owner_id=%s', (owner,))

    def complete_erasure(self, request_id, owner, replacement_hash, defaults, expires_at):
        self.connection.execute('DELETE FROM data_privacy_requests WHERE owner_id=%s AND id<>%s', (owner, request_id))
        self.connection.execute('UPDATE app_sessions SET token_hash=%s WHERE id=%s', (replacement_hash, owner))
        self.connection.execute('''UPDATE data_privacy_profiles SET state='erased',locale=%s,timezone=%s,
            tone='warm',reduced_motion=false,voice_mode='device',updated_at=now() WHERE owner_id=%s''',
            (defaults.default_locale, defaults.default_timezone, owner))
        self.connection.execute('''UPDATE data_privacy_requests SET owner_id=NULL,idempotency_hash=NULL,
            fingerprint=NULL,receipt_salt=NULL,export_bytes=NULL,state='completed',expires_at=%s,updated_at=now() WHERE id=%s''',
            (expires_at, request_id))

    def pending_requests(self, limit):
        return [row['id'] for row in self.connection.execute('''SELECT id FROM data_privacy_requests
            WHERE state IN ('queued','running') AND owner_id IS NOT NULL ORDER BY created_at,id LIMIT %s''', (limit,)).fetchall()]

    def purge_expired(self, now, metadata_cutoff):
        self.connection.execute("UPDATE data_privacy_requests SET export_bytes=NULL WHERE kind='export' AND expires_at<=%s", (now,))
        self.connection.execute('''DELETE FROM data_privacy_requests WHERE
            (owner_id IS NULL AND expires_at<=%s) OR (kind='export' AND created_at<=%s)''', (now, metadata_cutoff))


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
