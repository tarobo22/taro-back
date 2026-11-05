from psycopg import sql
from psycopg.rows import dict_row

from api.adapters.data_privacy_postgres import lock_owner, require_active_owner


SELECT_ENTRY = '''SELECT e.id,e.session_id AS owner_id,e.day AS local_day,e.mood,e.body,
    e.created_at,e.updated_at,e.version,s.source_reading_id,s.source_turn_id
    FROM journal_entries e LEFT JOIN journal_v2_sources s ON s.entry_id=e.id AND s.owner_id=e.session_id'''


class PostgresJournalV2Repository:
    def __init__(self, connection):
        self.connection = connection

    def lock(self, owner):
        lock_owner(self.connection, owner)

    def active(self, owner):
        require_active_owner(self.connection, owner)

    def get(self, owner, entry_id):
        return self.connection.execute(SELECT_ENTRY + ' WHERE e.session_id=%s AND e.id=%s', (owner, entry_id)).fetchone()

    def operation(self, owner, key_hash):
        return self.connection.execute('SELECT fingerprint,entry_id,expires_at FROM journal_v2_create_requests WHERE owner_id=%s AND key_hash=%s',
                                       (owner, key_hash)).fetchone()

    def remember(self, owner, key_hash, fingerprint, entry_id, expires_at):
        self.connection.execute('''INSERT INTO journal_v2_create_requests(owner_id,key_hash,fingerprint,entry_id,expires_at)
            VALUES(%s,%s,%s,%s,%s)''', (owner, key_hash, fingerprint, entry_id, expires_at))

    def expire_operation(self, owner, key_hash):
        self.connection.execute('UPDATE journal_v2_create_requests SET fingerprint=NULL,entry_id=NULL WHERE owner_id=%s AND key_hash=%s',
                                (owner, key_hash))

    def purge_expired_idempotency(self, now):
        self.connection.execute('''UPDATE journal_v2_create_requests SET fingerprint=NULL,entry_id=NULL
            WHERE expires_at<=%s AND (fingerprint IS NOT NULL OR entry_id IS NOT NULL)''', (now,))

    def create(self, owner, entry_id, values):
        self.connection.execute('INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,%s,%s,%s)',
                                (entry_id, owner, values['local_day'], values['mood'], values['body']))
        if values.get('source_reading_id') or values.get('source_turn_id'):
            self.connection.execute('''INSERT INTO journal_v2_sources(entry_id,owner_id,source_reading_id,source_turn_id)
                VALUES(%s,%s,%s,%s)''', (entry_id, owner, values.get('source_reading_id'), values.get('source_turn_id')))
        return self.get(owner, entry_id)

    def update(self, owner, entry_id, expected_version, values):
        assignments = sql.SQL(',').join(sql.SQL('{}=%s').format(sql.Identifier(key)) for key in values)
        result = self.connection.execute(sql.SQL('''UPDATE journal_entries SET {},version=version+1,updated_at=now()
            WHERE session_id=%s AND id=%s AND version=%s''').format(assignments),
            (*values.values(), owner, entry_id, expected_version))
        return self.get(owner, entry_id) if result.rowcount == 1 else None

    def list(self, owner, *, start=None, end=None, moods=None, after=None, limit=None):
        clauses, params = ['e.session_id=%s'], [owner]
        if start is not None:
            clauses.append('e.day>=%s'); params.append(start)
        if end is not None:
            clauses.append('e.day<=%s'); params.append(end)
        if moods is not None:
            clauses.append('e.mood=ANY(%s)'); params.append(moods)
        if after is not None:
            clauses.append('(e.created_at,e.id)<(%s,%s)'); params.extend(after)
        query = SELECT_ENTRY + ' WHERE ' + ' AND '.join(clauses) + ' ORDER BY e.created_at DESC,e.id DESC'
        if limit is not None:
            query += ' LIMIT %s'; params.append(limit)
        return self.connection.execute(query, params).fetchall()

    def deleted(self, owner, entry_id):
        return bool(self.connection.execute('SELECT 1 FROM journal_v2_deleted_entries WHERE owner_id=%s AND entry_id=%s',
                                           (owner, entry_id)).fetchone())

    def delete(self, owner, entry_id):
        result = self.connection.execute('DELETE FROM journal_entries WHERE session_id=%s AND id=%s', (owner, entry_id))
        if result.rowcount == 1:
            self.connection.execute('''INSERT INTO journal_v2_deleted_entries(owner_id,entry_id) VALUES(%s,%s)
                ON CONFLICT DO NOTHING''', (owner, entry_id))
        return result.rowcount == 1

    def erase_metadata(self, owner):
        for table in ('journal_v2_sources', 'journal_v2_create_requests', 'journal_v2_deleted_entries'):
            self.connection.execute(sql.SQL('DELETE FROM {} WHERE owner_id=%s').format(sql.Identifier(table)), (owner,))


class PostgresJournalV2UnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def repository_for(self, connection):
        return PostgresJournalV2Repository(connection)

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.journals = self.repository_for(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
