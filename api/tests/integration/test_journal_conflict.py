from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.journal import journal_router
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.journal import JournalUseCases
from api.domain.journal import JournalPolicy

pytestmark = pytest.mark.integration


@pytest.fixture
def editing_client(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('000[45]*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    policy = JournalPolicy(frozenset({'calm'}), 200)
    app = FastAPI()
    service = JournalUseCases(lambda: PostgresJournalUnitOfWork(pg_pool), policy, uuid4)
    app.include_router(journal_router(service, policy, lambda: owners[0]))
    with TestClient(app) as client:
        yield client


def test_stale_editor_cannot_overwrite_a_newer_entry(editing_client):
    client = editing_client
    initial = client.post('/v1/entries', json={'day': '2026-11-06', 'mood': 'calm', 'body': 'original'}).json()
    assert initial.get('version') == 1
    endpoint = '/v1/entries/' + initial['id']
    first = client.put(endpoint, json={'mood': 'calm', 'body': 'newer', 'expectedVersion': 1})
    assert first.status_code == 200 and first.json()['version'] == 2
    stale = client.put(endpoint, json={'mood': 'calm', 'body': 'stale draft', 'expectedVersion': 1})
    assert stale.status_code == 409
    assert client.get('/v1/entries').json()[0]['body'] == 'newer'


def test_concurrent_editors_with_the_same_version_have_only_one_winner(editing_client):
    client = editing_client
    initial = client.post('/v1/entries', json={'day': '2026-11-06', 'mood': 'calm', 'body': 'original'}).json()
    endpoint = '/v1/entries/' + initial['id']
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(client.put, endpoint, json={
            'mood': 'calm', 'body': value, 'expectedVersion': 1}) for value in ('editor a', 'editor b')]
        results = [future.result(timeout=10) for future in futures]
    assert sorted(result.status_code for result in results) == [200, 409]
    winner = next(result.json() for result in results if result.status_code == 200)
    assert client.get('/v1/entries').json()[0] == winner
