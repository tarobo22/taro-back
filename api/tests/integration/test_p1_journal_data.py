import json
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.journal import journal_router
from api.adapters.http.journal_summary import journal_summary_router
from api.adapters.http.journal_v2 import journal_v2_router
from api.adapters.journal_summary_postgres import PostgresJournalSummaryUnitOfWork
from api.adapters.journal_v2_postgres import PostgresJournalV2UnitOfWork
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.journal import JournalUseCases
from api.application.journal_summary import JournalSummaryUseCases
from api.application.journal_v2 import JournalV2UseCases
from api.domain.journal import JournalPolicy
from api.domain.journal_v2 import JournalV2Policy


pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


@pytest.fixture
def p1_data(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((ROOT / 'api/migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    content = json.loads((ROOT / 'shared/content.json').read_text(encoding='utf-8'))
    rules = json.loads((ROOT / 'shared/journal-policy.json').read_text(encoding='utf-8'))
    policy = JournalV2Policy.from_legacy(content['moods'], rules,
        json.loads((ROOT / 'shared/journal-v2-policy.json').read_text(encoding='utf-8')))
    summary = JournalSummaryUseCases(lambda: PostgresJournalSummaryUnitOfWork(pg_pool), policy)
    v2 = JournalV2UseCases(lambda: PostgresJournalV2UnitOfWork(pg_pool), policy)
    legacy_policy = JournalPolicy(frozenset(content['moods']), rules['maxBodyLength'])
    legacy = JournalUseCases(lambda: PostgresJournalUnitOfWork(pg_pool), legacy_policy, uuid4)
    current = [owners[0]]
    app = FastAPI()
    app.include_router(journal_summary_router(summary, lambda: current[0]))
    app.include_router(journal_v2_router(v2, lambda: current[0]))
    app.include_router(journal_router(legacy, legacy_policy, lambda: current[0]))
    with TestClient(app) as client:
        yield client, current, owners, pg_pool, summary


def summary_get(client, start='2026-10-08', end='2026-11-06'):
    return client.get('/v2/journal-summary', params={'from': start, 'to': end})


def test_summary_counts_v1_v2_shared_rows_inclusive_local_day_and_owner_isolation(p1_data):
    client, current, owners, pool, _ = p1_data
    # The created_at is intentionally outside the selected range; diary day is authoritative.
    with pool.connection() as connection:
        for day, mood, owner in [('2026-10-07', 'happy', owners[0]), ('2026-10-08', 'calm', owners[0]),
                                ('2026-11-06', 'thinking', owners[0]), ('2026-11-07', 'talk', owners[0]),
                                ('2026-11-06', 'happy', owners[1])]:
            connection.execute('INSERT INTO journal_entries(id,session_id,day,mood,created_at) VALUES(%s,%s,%s,%s,%s)',
                               (uuid4(), owner, day, mood, datetime(2020, 1, 1, tzinfo=timezone.utc)))
    legacy = client.post('/v1/entries', json={'day': '2026-10-08', 'mood': '편안해요', 'body': 'private v1 diary'})
    assert legacy.status_code == 201
    v2 = client.post('/v2/journal-entries', json={'local_day': '2026-11-06', 'mood': 'settled', 'body': 'private v2 diary'},
                     headers={'X-Idempotency-Key': str(uuid4())})
    assert v2.status_code == 201
    result = summary_get(client)
    assert result.status_code == 200
    assert result.json() == {'range_start': '2026-10-08', 'range_end': '2026-11-06', 'entry_count': 4,
        'days_with_entries': ['2026-10-08', '2026-11-06'],
        'mood_counts': {'calm': 2, 'happy': 0, 'thinking': 1, 'settled': 1, 'talk': 0}}
    assert summary_get(client, '2026-11-06', '2026-11-06').json()['entry_count'] == 2
    current[0] = owners[1]
    assert summary_get(client).json()['mood_counts']['happy'] == 1
    assert summary_get(client).json()['entry_count'] == 1


def test_summary_empty_range_is_zero_and_get_does_not_create_ai_or_usage_rows(p1_data):
    client, _, owners, pool, _ = p1_data
    with pool.connection() as connection:
        tables = [row['tablename'] for row in connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND (tablename LIKE '%job%' OR tablename LIKE '%usage%' OR tablename LIKE '%attempt%' OR tablename LIKE '%consent%')")]
        before = {table: connection.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] for table in tables}
    result = summary_get(client).json()
    assert result['entry_count'] == 0 and result['days_with_entries'] == []
    assert result['mood_counts'] == dict.fromkeys(('calm', 'happy', 'thinking', 'settled', 'talk'), 0)
    with pool.connection() as connection:
        assert before == {table: connection.execute(f'SELECT count(*) AS n FROM {table}').fetchone()['n'] for table in tables}
        assert not connection.execute('SELECT 1 FROM journal_entries WHERE session_id=%s', (owners[0],)).fetchone()


def test_summary_rejects_reversed_range_and_deleting_or_erased_owners(p1_data):
    client, _, owners, pool, _ = p1_data
    assert summary_get(client, '2026-11-07', '2026-11-06').status_code == 422
    with pool.connection() as connection:
        connection.execute("INSERT INTO data_privacy_profiles(owner_id,state) VALUES(%s,'deleting')", (owners[0],))
    assert summary_get(client).status_code == 409
    with pool.connection() as connection:
        connection.execute("UPDATE data_privacy_profiles SET state='erased' WHERE owner_id=%s", (owners[0],))
    assert summary_get(client).status_code == 404
