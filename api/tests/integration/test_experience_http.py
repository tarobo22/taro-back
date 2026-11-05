import json
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.experience_postgres import PostgresExperienceUnitOfWork
from api.adapters.http.experience import experience_router
from api.adapters.local_guidance import LocalGuidance
from api.application.experience import ExperienceUseCases
from api.domain.experience import ExperiencePolicy

pytestmark = pytest.mark.integration


@pytest.fixture
def experience(pg_pool, owners):
    root = Path(__file__).parents[2]
    content = json.loads((root.parent / 'shared/content.json').read_text(encoding='utf-8'))
    rules = json.loads((root.parent / 'shared/experience-policy.json').read_text(encoding='utf-8'))
    policy = ExperiencePolicy(tuple(content['topics']), tuple(content['cards']), rules['spreads'],
                              tuple(content['talkQuestions']), rules['maxTarotQuestion'], rules['maxCounselQuestion'])
    with pg_pool.connection() as connection:
        for migration in sorted((root / 'migrations').glob('000[23]*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    guide = LocalGuidance()
    service = ExperienceUseCases(lambda: PostgresExperienceUnitOfWork(pg_pool), policy, guide, guide)
    current = [owners[0]]
    app = FastAPI()
    app.include_router(experience_router(service, {}, lambda: current[0]))
    with TestClient(app) as client:
        yield client, current, content['topics'][0]
    with pg_pool.connection() as connection:
        for table in ('experience_operations', 'daily_readings', 'tarot_readings', 'counsel_sessions'):
            connection.execute(f'DELETE FROM {table} WHERE session_id=ANY(%s)', (list(owners),))


def test_reading_is_idempotent_and_private(experience, owners):
    client, current, topic = experience
    payload = {'topic': topic, 'cardIds': [0, 3, 7], 'requestId': str(uuid4())}
    first = client.post('/v1/readings', json=payload)
    assert first.status_code == 201
    assert first.json() == client.post('/v1/readings', json=payload).json()
    assert len(client.get('/v1/readings').json()) == 1
    assert client.post('/v1/readings', json={**payload, 'cardIds': [1, 3, 7]}).status_code == 409
    current[0] = owners[1]
    assert client.get('/v1/readings').json() == []


def test_daily_is_stable_and_in_history(experience):
    client, current, topic = experience
    payload = {'topic': topic, 'day': '2026-11-06'}
    first = client.post('/v1/daily', json=payload)
    assert first.status_code == 200
    assert first.json() == client.post('/v1/daily', json=payload).json()
    assert len(first.json()['cards']) == 1
    assert len(client.get('/v1/readings').json()) == 1


def test_consent_and_conversation_ownership(experience, owners):
    client, current, topic = experience
    payload = {'topic': topic, 'question': '마음을 정리하고 싶어요', 'answers': [True, False, True],
               'requestId': str(uuid4()), 'useAI': True, 'consent': False}
    assert client.post('/v1/counsel', json=payload).status_code == 422
    first = client.post('/v1/counsel', json={**payload, 'consent': True})
    assert first.status_code == 201 and first.json()['source'] == 'openai'
    repeat = client.post('/v1/counsel', json={**payload, 'consent': True})
    assert repeat.json() == first.json()
    parent = first.json()['id']
    current[0] = owners[1]
    assert client.post('/v1/counsel', json={**payload, 'requestId': str(uuid4()), 'consent': True, 'parentId': parent}).status_code == 404
    assert client.get('/v1/counsel').json() == []
