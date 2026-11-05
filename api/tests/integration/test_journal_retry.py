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
def retry_client(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('0004*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    policy = JournalPolicy(frozenset({'calm'}), 200)
    service = JournalUseCases(lambda: PostgresJournalUnitOfWork(pg_pool), policy, uuid4)
    current_owner = [owners[0]]
    app = FastAPI()
    app.include_router(journal_router(service, policy, lambda: current_owner[0]))
    with TestClient(app) as client:
        yield client, current_owner


def test_lost_create_response_can_be_retried_without_duplicate_or_cross_owner_leak(retry_client, owners):
    client, current_owner = retry_client
    payload = {'day': '2026-11-06', 'mood': 'calm', 'body': 'keep my words', 'requestId': str(uuid4())}
    committed = client.post('/v1/entries', json=payload)
    replay = client.post('/v1/entries', json=payload)
    assert committed.status_code == replay.status_code == 201
    assert replay.json() == committed.json()
    assert len(client.get('/v1/entries').json()) == 1
    assert client.post('/v1/entries', json={**payload, 'body': 'different words'}).status_code == 409
    current_owner[0] = owners[1]
    other = client.post('/v1/entries', json=payload)
    assert other.status_code == 201 and other.json()['id'] != committed.json()['id']
    assert len(client.get('/v1/entries').json()) == 1


def test_concurrent_create_retries_commit_one_record(retry_client):
    client, _ = retry_client
    payload = {'day': '2026-11-06', 'mood': 'calm', 'body': 'once', 'requestId': str(uuid4())}
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(client.post, '/v1/entries', json=payload) for _ in range(2)]
        responses = [future.result(timeout=10) for future in futures]
    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    assert len(client.get('/v1/entries').json()) == 1
