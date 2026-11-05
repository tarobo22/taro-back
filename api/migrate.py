"""Explicit, transactional SQL migrations. Never changes schema during app startup."""
from pathlib import Path

import psycopg

from api.config import Settings


def migrate():
    with psycopg.connect(Settings.from_env().database_url) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(8808001)')
        connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
        applied = {row[0] for row in connection.execute('SELECT version FROM schema_migrations')}
        for migration in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
            if migration.name not in applied:
                connection.execute(migration.read_text(encoding='utf-8'))
                connection.execute('INSERT INTO schema_migrations(version) VALUES(%s)', (migration.name,))
                print(f'Applied {migration.name}')


if __name__ == '__main__':
    migrate()
