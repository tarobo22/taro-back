"""Short PostgreSQL units of work; workers close these before external I/O."""
from psycopg.rows import dict_row


class PostgresAIJobsUnitOfWork:
    def __init__(self, pool):
        self.pool = pool

    def __enter__(self):
        self.connection = self.pool.getconn()
        self.connection.row_factory = dict_row
        return self

    def commit(self):
        self.connection.commit()

    def __exit__(self, *exc):
        try:
            self.connection.rollback()
        finally:
            self.pool.putconn(self.connection)
