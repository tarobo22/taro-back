from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.story import story_router
from api.adapters.story_catalog import load_story_catalog
from api.application.story import StoryService
from api.tests.unit.test_story import State, UnitOfWork


def test_explicit_start_choice_resume_restart_and_owner_guard_use_the_actual_http_contract():
    state, owner = State(), [uuid4()]
    service = StoryService(lambda: UnitOfWork(state), load_story_catalog())
    app = FastAPI(); app.include_router(story_router(service, lambda: owner[0]))
    with TestClient(app) as client:
        episodes = client.get('/v2/story-episodes'); assert episodes.status_code == 200
        episode = episodes.json()['items'][0]
        assert client.get('/v2/story-progress', params={'episode_id': episode['id']}).json() == {'run': None}
        start = client.post('/v2/story-runs', json={'episode_id': episode['id'], 'episode_version': episode['version']}, headers={'X-Idempotency-Key': str(uuid4())})
        assert start.status_code == 201; run = start.json()
        advanced = client.post('/v2/story-runs/'+run['id']+'/choices', json={'expected_version': run['version'], 'node_id': run['current_node']['id'], 'answer': False}, headers={'X-Idempotency-Key': str(uuid4())})
        assert advanced.status_code == 200 and advanced.json()['choice_count'] == 1
        assert client.get('/v2/story-progress', params={'episode_id': episode['id']}).json()['run']['version'] == 2
        assert client.post('/v2/story-runs/'+run['id']+'/restart', json={'expected_version': 1}, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 409
        owner[0] = uuid4()
        assert client.get('/v2/story-runs/'+run['id']).status_code == 404


def test_http_rejects_untyped_answers_missing_identity_and_extra_fields_without_a_write():
    state, owner = State(), uuid4(); service = StoryService(lambda: UnitOfWork(state), load_story_catalog())
    app = FastAPI(); app.include_router(story_router(service, lambda: owner))
    with TestClient(app) as client:
        body = {'episode_id': 'window-pause', 'episode_version': '1.0.0'}
        assert client.post('/v2/story-runs', json=body).status_code == 422
        assert client.post('/v2/story-runs', json={**body, 'reward': 10}, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 422
        assert not state.rows
