import os
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@pytest.fixture(scope='session')
def pg_pool():
    dsn = os.environ.get('YEOUN_TEST_DATABASE_URL')
    if not dsn or not conninfo_to_dict(dsn).get('dbname', '').endswith('_test'):
        raise RuntimeError('Set YEOUN_TEST_DATABASE_URL to an isolated database ending in _test')
    with ConnectionPool(dsn, min_size=1, max_size=3, kwargs={'row_factory': dict_row}) as pool:
        pool.wait(timeout=15)
        with pool.connection() as connection:
            connection.execute((Path(__file__).parents[1] / 'schema.sql').read_text(encoding='utf-8'))
        yield pool


@pytest.fixture
def owners(pg_pool):
    ids = (uuid4(), uuid4())
    with pg_pool.connection() as connection:
        for owner in ids:
            connection.execute('INSERT INTO app_sessions(id,token_hash) VALUES(%s,%s)', (owner, str(owner)))
    yield ids
    with pg_pool.connection() as connection:
        connection.execute('DELETE FROM journal_entries WHERE session_id=ANY(%s)', (list(ids),))
        connection.execute('DELETE FROM app_sessions WHERE id=ANY(%s)', (list(ids),))
