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


def test_latest_entry_lookup_returns_current_version_only_to_its_owner(pg_pool, owners):
    policy = JournalPolicy(frozenset({'calm'}), 200)
    service = JournalUseCases(lambda: PostgresJournalUnitOfWork(pg_pool), policy, uuid4)
    saved = service.create(owners[0], date(2026, 11, 6), 'calm', 'original')
    updated = service.update(owners[0], saved.id, 'calm', 'newest', 1)
    owner = [owners[0]]
    app = FastAPI()
    app.include_router(journal_router(service, policy, lambda: owner[0]))
    with TestClient(app) as client:
        response = client.get('/v1/entries/' + str(saved.id))
        assert response.status_code == 200
        assert response.json()['body'] == updated.body
        assert response.json()['version'] == 2
        owner[0] = owners[1]
        assert client.get('/v1/entries/' + str(saved.id)).status_code == 404
        assert client.get('/v1/entries/' + str(uuid4())).status_code == 404
