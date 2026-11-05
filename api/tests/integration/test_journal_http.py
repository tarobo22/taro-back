from datetime import date
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.journal import journal_router
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.journal import JournalUseCases
from api.domain.journal import JournalPolicy

pytestmark = pytest.mark.integration


def test_http_contract_crud_validation_and_owner_scope(pg_pool, owners):
    policy = JournalPolicy(frozenset({'calm', 'curious'}), 40)
    use_cases = JournalUseCases(lambda: PostgresJournalUnitOfWork(pg_pool), policy, uuid4)
    current_owner = [owners[0]]
    app = FastAPI()
    app.include_router(journal_router(use_cases, policy, lambda: current_owner[0]))
    with TestClient(app) as client:
        response = client.post('/v1/entries', json={'day': '2026-11-06', 'mood': 'calm', 'body': '  hello  '})
        assert response.status_code == 201
        entry = response.json()
        assert set(entry) == {'id', 'day', 'mood', 'body', 'created_at', 'updated_at', 'version'}
        assert entry['version'] == 1
        assert entry['body'] == 'hello' and entry['day'] == '2026-11-06'
        assert client.get('/v1/entries').json() == [entry]
        path = '/v1/entries/' + entry['id']
        edited = client.put(path, json={'mood': 'curious', 'body': 'after'})
        assert edited.status_code == 200 and edited.json()['day'] == entry['day']
        for payload in ({'day': 'bad', 'mood': 'calm'}, {'day': '2026-11-06', 'mood': 'unknown'},
                        {'day': '2026-11-06', 'mood': 'calm', 'body': 'x' * 41}):
            assert client.post('/v1/entries', json=payload).status_code == 422
        current_owner[0] = owners[1]
        assert client.get('/v1/entries').json() == []
        assert client.put(path, json={'mood': 'calm', 'body': 'attack'}).status_code == 404
        assert client.delete(path).json() == {'deleted': False}
        current_owner[0] = owners[0]
        assert client.delete(path).json() == {'deleted': True}
        assert client.get('/v1/entries').json() == []


def test_http_policy_is_from_injected_catalog(pg_pool, owners):
    policy = JournalPolicy(frozenset({'calm', 'curious'}), 40)
    use_cases = JournalUseCases(lambda: PostgresJournalUnitOfWork(pg_pool), policy, uuid4)
    app = FastAPI()
    app.include_router(journal_router(use_cases, policy, lambda: owners[0]))
    with TestClient(app) as client:
        response = client.get('/v1/journal-policy')
        assert response.status_code == 200
        assert response.json() == {'moods': ['calm', 'curious'], 'maxBodyLength': 40}
