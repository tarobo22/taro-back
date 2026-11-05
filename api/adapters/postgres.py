from dataclasses import asdict
from datetime import date, datetime
import hashlib
import json
from uuid import UUID

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from api.domain.journal import JournalEntry, JournalRequestConflict, JournalVersionConflict


def entry_from_row(row):
    return JournalEntry(**row) if row else None


FIELDS = 'id, session_id AS owner_id, day, mood, body, created_at, updated_at, version'


class PostgresJournalRepository:
    def __init__(self, connection):
        self.connection = connection

    def add(self, entry):
        row = self.connection.execute(
            f'INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,%s,%s,%s) RETURNING {FIELDS}',
            (entry.id, entry.owner_id, entry.day, entry.mood, entry.body)).fetchone()
        return entry_from_row(row)

    def add_once(self, entry, request_id):
        payload = {'day': entry.day.isoformat(), 'mood': entry.mood, 'body': entry.body}
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                                (f'journal:{entry.owner_id}:{request_id}',))
        previous = self.connection.execute(
            'SELECT fingerprint,result FROM journal_create_requests WHERE session_id=%s AND request_id=%s',
            (entry.owner_id, request_id)).fetchone()
        if previous:
            if previous['fingerprint'] != fingerprint:
                raise JournalRequestConflict('request_id_reused')
            value = previous['result']
            return JournalEntry(**{**value, 'id': UUID(value['id']), 'owner_id': UUID(value['owner_id']),
                'day': date.fromisoformat(value['day']), 'created_at': datetime.fromisoformat(value['created_at']),
                'updated_at': datetime.fromisoformat(value['updated_at'])})
        saved = self.add(entry)
        snapshot = json.loads(json.dumps(asdict(saved), default=str))
        self.connection.execute(
            'INSERT INTO journal_create_requests(session_id,request_id,fingerprint,result) VALUES(%s,%s,%s,%s)',
            (entry.owner_id, request_id, fingerprint, Jsonb(snapshot)))
        return saved

    def get(self, owner_id, entry_id):
        row = self.connection.execute(
            f'SELECT {FIELDS} FROM journal_entries WHERE session_id=%s AND id=%s',
            (owner_id, entry_id)).fetchone()
        return entry_from_row(row)

    def list(self, owner_id):
        rows = self.connection.execute(
            f'SELECT {FIELDS} FROM journal_entries WHERE session_id=%s ORDER BY day DESC,created_at DESC LIMIT 100',
            (owner_id,)).fetchall()
        return [entry_from_row(row) for row in rows]

    def update(self, entry):
        row = self.connection.execute(
            f'UPDATE journal_entries SET mood=%s,body=%s,updated_at=now(),version=version+1 WHERE session_id=%s AND id=%s AND version=%s RETURNING {FIELDS}',
            (entry.mood, entry.body, entry.owner_id, entry.id, entry.version)).fetchone()
        if row is None:
            from api.application.journal import JournalNotFound
            if self.get(entry.owner_id, entry.id) is not None:
                raise JournalVersionConflict('stale_entry')
            raise JournalNotFound(entry.id)
        return entry_from_row(row)

    def delete(self, owner_id, entry_id):
        return self.connection.execute(
            'DELETE FROM journal_entries WHERE session_id=%s AND id=%s',
            (owner_id, entry_id)).rowcount == 1


class PostgresJournalUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.journals = PostgresJournalRepository(self.connection)
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            # Application use cases commit explicitly. All remaining work is discarded.
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
