from psycopg.rows import dict_row

from api.adapters.data_privacy_postgres import lock_owner, require_active_owner
from api.domain.journal_summary import JournalMoodBucket


class PostgresJournalSummaryRepository:
    def __init__(self, connection):
        self.connection = connection

    def lock(self, owner):
        lock_owner(self.connection, owner)

    def active(self, owner):
        require_active_owner(self.connection, owner)

    def groups(self, owner, start, end):
        rows = self.connection.execute('''SELECT day AS local_day,mood,count(*) AS entry_count
            FROM journal_entries WHERE session_id=%s AND day>=%s AND day<=%s
            GROUP BY day,mood ORDER BY day,mood''', (owner, start, end)).fetchall()
        return [JournalMoodBucket(row['local_day'], row['mood'], row['entry_count']) for row in rows]


class PostgresJournalSummaryUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        self.summary = PostgresJournalSummaryRepository(self.connection)
        return self

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
