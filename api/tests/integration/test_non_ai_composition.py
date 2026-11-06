"""Verify the real composition boundary, not only isolated social/billing/story routers."""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from api.config import Settings
from api.domain.social_auth import VerifiedSocialIdentity, digest, pkce_challenge
from api.main import create_app

pytestmark = pytest.mark.integration


def test_two_devices_share_the_account_preserve_guests_and_revocation_cannot_fall_back(pg_pool):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    namespace = str(uuid4());returned = 'http://127.0.0.1:8827/auth/complete';verifier = 'v' * 64
    class Provider:
        def available(self, provider):return True
        def authorization_url(self, provider, **values):return 'https://provider.test/authorize?' + urlencode(values)
        def exchange(self, provider, code, **values):return VerifiedSocialIdentity(provider, digest(namespace + provider))
    app = create_app(Settings(os.environ['YEOUN_TEST_DATABASE_URL'], ()), result_provider=None,
        transcription_provider=None, workers_enabled=False, social_provider=Provider(), social_return_uris=(returned,))
    def guest(client):
        response = client.post('/v2/sessions', json={'platform': 'web'})
        assert response.status_code == 201
        token = response.json()['access_token'];client.headers['Authorization'] = 'Bearer ' + token
        return response.json()['user_id'], token
    def login(client, provider='google', intent='sign_in'):
        started = client.post('/v2/auth/social/' + provider + '/start', json={'return_uri': returned,
            'code_challenge': pkce_challenge(verifier), 'platform': 'web', 'intent': intent})
        assert started.status_code == 201, started.text
        state = parse_qs(urlsplit(started.json()['authorization_url']).query)['state'][0]
        callback = client.get('/v2/auth/social/' + provider + '/callback', params={'state': state, 'code': 'generated-fixture-code'}, follow_redirects=False)
        assert callback.status_code == 303 and 'access_token' not in callback.headers['location']
        ticket = parse_qs(urlsplit(callback.headers['location']).fragment)['auth_ticket'][0]
        grant = client.post('/v2/auth/social/exchange', json={'ticket': ticket, 'code_verifier': verifier})
        assert grant.status_code == 201, grant.text
        client.headers['Authorization'] = 'Bearer ' + grant.json()['access_token']
        return grant.json()
    with TestClient(app) as first, TestClient(app) as second:
        owner, original_guest_token = guest(first)
        saved = first.post('/v2/journal-entries', json={'local_day': '2026-11-06', 'mood': 'calm', 'body': '내 첫 기기의 기록'}, headers={'X-Idempotency-Key': str(uuid4())})
        assert saved.status_code == 201
        initial = login(first)
        assert initial['user_id'] == owner and first.get('/v2/bootstrap').json()['user']['kind'] == 'member'
        renewed = first.post('/v2/sessions/refresh')
        assert renewed.status_code == 200 and renewed.json()['session_id'] == initial['session_id']
        linked = login(first, 'apple', 'link')
        assert linked['user_id'] == owner and first.get('/v2/auth/account').json()['providers'] == ['apple', 'google']
        second_guest, second_guest_token = guest(second)
        local = second.post('/v2/journal-entries', json={'local_day': '2026-11-06', 'mood': 'happy', 'body': '다른 게스트의 기록'}, headers={'X-Idempotency-Key': str(uuid4())})
        assert local.status_code == 201
        other = login(second)
        assert other['user_id'] == owner and other['previous_user_id'] == second_guest
        entries = second.get('/v2/journal-entries').json()['items']
        assert [e['body'] for e in entries] == ['내 첫 기기의 기록']
        assert second.get('/v2/journal-entries/' + local.json()['id']).status_code == 404
        second.headers['Authorization'] = 'Bearer ' + second_guest_token
        assert second.get('/v2/journal-entries/' + local.json()['id']).json()['body'] == '다른 게스트의 기록'
        first.headers['Authorization'] = 'Bearer ' + linked['access_token']
        assert first.post('/v2/auth/logout-all').status_code == 204
        for token in (original_guest_token, initial['access_token'], linked['access_token'], other['access_token']):
            first.headers['Authorization'] = 'Bearer ' + token
            assert first.get('/v2/me').status_code == 401


def test_story_and_unconfigured_store_are_connected_to_the_real_guest_api(pg_pool):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    app = create_app(Settings(os.environ['YEOUN_TEST_DATABASE_URL'], ()), result_provider=None,
                     transcription_provider=None, workers_enabled=False)
    with TestClient(app) as client:
        grant = client.post('/v2/sessions', json={'platform': 'android'}).json()
        client.headers['Authorization'] = 'Bearer ' + grant['access_token']
        catalog = client.get('/v2/billing/catalog').json()
        assert catalog['available'] is False and catalog['products'] == [] and catalog['can_purchase'] is False
        assert client.get('/v2/billing/entitlements').json()['items'] == []
        episode = client.get('/v2/story-episodes').json()['items'][0]
        started = client.post('/v2/story-runs', json={'episode_id': episode['id'], 'episode_version': episode['version']},
                              headers={'X-Idempotency-Key': str(uuid4())})
        assert started.status_code == 201
        run = started.json()
        for answer in (False, True, False):
            response = client.post('/v2/story-runs/' + run['id'] + '/choices', json={'node_id': run['current_node']['id'],
                'expected_version': run['version'], 'answer': answer}, headers={'X-Idempotency-Key': str(uuid4())})
            assert response.status_code == 200, response.text
            run = response.json()
        assert run['state'] == 'completed' and run['ending'] and run['choice_count'] == 3
        assert client.get('/v2/story-progress', params={'episode_id': episode['id']}).json()['run']['id'] == run['id']
