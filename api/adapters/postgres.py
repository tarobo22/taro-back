from psycopg.rows import dict_row

from api.domain.journal import JournalEntry


def entry_from_row(row):
    return JournalEntry(**row) if row else None


FIELDS = 'id, session_id AS owner_id, day, mood, body, created_at, updated_at'


class PostgresJournalRepository:
    def __init__(self, connection):
        self.connection = connection

    def add(self, entry):
        row = self.connection.execute(
            f'INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,%s,%s,%s) RETURNING {FIELDS}',
            (entry.id, entry.owner_id, entry.day, entry.mood, entry.body)).fetchone()
        return entry_from_row(row)

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
            f'UPDATE journal_entries SET mood=%s,body=%s,updated_at=now() WHERE session_id=%s AND id=%s RETURNING {FIELDS}',
            (entry.mood, entry.body, entry.owner_id, entry.id)).fetchone()
        if row is None:
            from api.application.journal import JournalNotFound
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
