"""Exercise real composition, PostgreSQL and workers without an external AI request."""
from io import BytesIO
import json
import os
from pathlib import Path
import time
from uuid import UUID, uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest

from api.adapters.ai_audio import PrivateAudioStore
from api.config import Settings
from api.main import create_app

pytestmark = pytest.mark.integration


class ResultProvider:
    model = 'integration-fake'

    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def generate(self, payload):
        self.calls.append(payload)
        return {'kind': payload['kind'], 'empathy': '마음을 나눠 주셔서 고마워요.',
                'summary': '오늘의 생각을 정리해 봐요.',
                'card_insights': [{'card_id': card['card_id'], 'position': card['position'],
                                   'meaning': '현재의 선택', 'reflection': '작은 행동을 살펴봐요.'}
                                  for card in payload['cards']],
                'advice': '할 수 있는 일을 하나 골라 보세요.', 'actions': ['작은 목표 하나 적기'],
                'expression': 'encourage', 'follow_up_questions': [], 'safety_mode': 'standard'}


@pytest.fixture
def app_flow(pg_pool, tmp_path):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    provider, owners, requests = ResultProvider(), [], []

    def make_client(*, workers=True):
        settings = Settings(os.environ['YEOUN_TEST_DATABASE_URL'], ('http://localhost',),
                            worker_poll_seconds=0.02)
        app = create_app(settings, result_provider=provider, transcription_provider=None,
                         audio_store=PrivateAudioStore(tmp_path / 'private-audio'), workers_enabled=workers)
        return TestClient(app)

    def start(client):
        response = client.post('/v2/sessions', json={'platform': 'web', 'timezone': 'Asia/Seoul'})
        assert response.status_code == 201, response.text
        grant = response.json()
        owners.append(UUID(grant['user_id']))
        client.headers['Authorization'] = 'Bearer ' + grant['access_token']
        return grant

    yield make_client, start, provider, requests
    with pg_pool.connection() as connection:
        connection.execute('DELETE FROM data_privacy_requests WHERE id=ANY(%s)', (requests,))
        connection.execute('DELETE FROM app_sessions WHERE id=ANY(%s)', (owners,))


def post(client, path, body=None):
    return client.post(path, json=body, headers={'X-Idempotency-Key': str(uuid4())})


def wait_job(client, job):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        response = client.get(job['status_url'])
        assert response.status_code == 200, response.text
        state = response.json()
        if state['state'] in ('succeeded', 'failed', 'cancelled'):
            return state
        time.sleep(0.025)
    pytest.fail('Accepted work did not reach a committed terminal state')


def test_bootstrap_session_contract_and_cors_never_allocate_on_invalid_auth(app_flow, pg_pool):
    make_client, start, _, _ = app_flow
    with make_client(workers=False) as client:
        health = client.get('/v2/health')
        assert health.status_code == 200
        assert set(health.json()) == {'ok', 'service', 'api_version'}
        unauthorized = client.get('/v2/bootstrap')
        assert unauthorized.status_code == 401
        error = unauthorized.json()
        assert set(error) == {'error', 'request_id'}
        assert error['error']['code'] == 'SESSION_INVALID'
        UUID(error['request_id'])
        grant = start(client)
        bootstrap = client.get('/v2/bootstrap').json()
        assert set(bootstrap) == {'api_version', 'policy_version', 'server_time', 'user', 'consents', 'ota_policy', 'limits'}
        assert bootstrap['api_version'] == '2.0' and bootstrap['user']['id'] == grant['user_id']
        assert bootstrap['consents'] == [] and bootstrap['policy_version'] == '2026-11-05'
        assert bootstrap['limits'] == {'daily_ai_requests': 20, 'question_max_chars': 500,
            'journal_max_chars': 4000, 'audio_max_seconds': 60, 'audio_max_bytes': 10485760}
        renewed = client.post('/v2/sessions/refresh').json()
        assert renewed['session_id'] == grant['session_id']
        assert client.get('/v1/entries').status_code == 200
        preflight = client.options('/v2/journal-entries/' + str(uuid4()), headers={
            'Origin': 'http://localhost', 'Access-Control-Request-Method': 'PATCH',
            'Access-Control-Request-Headers': 'authorization,x-idempotency-key,x-data-receipt'})
        assert preflight.status_code == 200
        invalid = client.post('/v2/sessions', json={'platform': 'web', 'timezone': 'Wrong/Zone'})
        assert invalid.status_code == 422 and invalid.json()['error']['retryable'] is False


def test_real_worker_connects_draw_context_result_journal_feedback_export_and_erase(app_flow):
    make_client, start, provider, requests = app_flow
    with make_client() as client:
        grant = start(client)
        deck = client.get('/v2/decks').json()['items'][0]
        reading = post(client, '/v2/readings', {'kind': 'three_card', 'topic': 'self',
            'deck_version_id': deck['id'], 'question': '오늘 무엇을 해볼까요?'}).json()
        assert len(reading['slots']) == 12 and reading['selected_cards'] == []
        assert all(set(slot) == {'id', 'index'} for slot in reading['slots'])
        selected = post(client, f"/v2/readings/{reading['id']}/selection", {
            'expected_version': reading['version'], 'slot_ids': [slot['id'] for slot in reading['slots'][2:5]]}).json()
        assert len(selected['selected_cards']) == 3
        basic = client.get(f"/v2/readings/{reading['id']}/basic").json()
        assert basic['cards'] == selected['selected_cards'] and basic['source'] == 'basic'
        journal = post(client, '/v2/journal-entries', {'local_day': '2026-11-06', 'mood': 'calm',
            'body': '내가 선택한 기록만 전달해요.', 'source_reading_id': reading['id']}).json()
        hidden = post(client, '/v2/journal-entries', {'local_day': '2026-11-06', 'mood': 'thinking',
            'body': '이 기록은 전달하지 않아요.'}).json()
        bootstrap = client.get('/v2/bootstrap').json()
        ai_consent = post(client, '/v2/me/consents', {'kind': 'ai_processing',
            'policy_version': bootstrap['policy_version'], 'granted': True}).json()
        post(client, '/v2/me/consents', {'kind': 'journal_context',
            'policy_version': bootstrap['policy_version'], 'granted': True})
        interpretation = post(client, f"/v2/readings/{reading['id']}/interpretations", {
            'expected_version': selected['version'], 'consent_event_id': ai_consent['id'],
            'context_entry_ids': [journal['id']]})
        assert interpretation.status_code == 202, interpretation.text
        completed = wait_job(client, interpretation.json())
        assert completed['state'] == 'succeeded', completed
        ready = client.get(completed['result_url']).json()
        assert ready['state'] == 'ready' and ready['result']['kind'] == 'tarot'
        assert ready['selected_cards'] == selected['selected_cards']
        sent = json.dumps(provider.calls[0], ensure_ascii=False)
        assert journal['body'] in sent and hidden['body'] not in sent
        assert len(provider.calls) == 1
        feedback = post(client, '/v2/feedback', {'reading_id': reading['id'], 'rating': 'helpful',
            'reason': '행동이 구체적이에요.', 'comment': ''})
        assert feedback.status_code == 201, feedback.text
        export = post(client, '/v2/me/data-requests', {'kind': 'export'})
        assert export.status_code == 202, export.text
        request = export.json()
        requests.append(UUID(request['id']))
        status = client.get('/v2/data-requests/' + request['id']).json()
        assert status['state'] == 'completed' and status['download_available'] is True
        download = client.get('/v2/data-requests/' + request['id'] + '/download')
        assert download.status_code == 200 and download.headers['cache-control'] == 'no-store'
        with ZipFile(BytesIO(download.content)) as archive:
            data = json.loads(archive.read('data.json'))
        assert 'v2_readings' in data and 'v2_journal_entries' in data and 'consents' in data
        assert grant['access_token'] not in download.content.decode('latin1')
        erased = post(client, '/v2/me/data-requests', {'kind': 'erase', 'confirmation': '내 기록 모두 삭제'})
        assert erased.status_code == 202, erased.text
        receipt = erased.json()
        requests.append(UUID(receipt['id']))
        status = client.get('/v2/data-requests/' + receipt['id'], headers={'X-Data-Receipt': receipt['receipt']})
        assert status.status_code == 200 and status.json()['state'] == 'completed'
        assert client.get('/v2/bootstrap').status_code == 401
        assert client.get('/v1/entries').status_code == 401
        assert client.get('/v2/data-requests/' + receipt['id'], headers={'X-Data-Receipt': 'wrong'}).status_code == 404
        assert client.get('/v2/data-requests/' + request['id'] + '/download',
                          headers={'X-Data-Receipt': request['receipt']}).status_code == 401


def test_local_queued_turn_completes_without_provider_or_consent_and_cancel_is_explicit(app_flow):
    make_client, start, provider, _ = app_flow
    with make_client() as client:
        start(client)
        conversation = post(client, '/v2/conversations', {'topic': 'self', 'tone': 'giga'}).json()
        accepted = post(client, f"/v2/conversations/{conversation['id']}/turns", {
            'client_message_id': str(uuid4()), 'input_source': 'text', 'text': '차근차근 시작할래요.', 'use_ai': False})
        assert accepted.status_code == 202, accepted.text
        complete = wait_job(client, accepted.json())
        assert complete['state'] == 'succeeded'
        result = client.get(complete['result_url']).json()
        assert result['assistant_result']['actions'] and not provider.calls
    with make_client(workers=False) as client:
        start(client)
        conversation = post(client, '/v2/conversations', {'topic': 'work', 'tone': 'warm'}).json()
        accepted = post(client, f"/v2/conversations/{conversation['id']}/turns", {
            'client_message_id': str(uuid4()), 'input_source': 'text', 'text': '잠깐 기다릴게요.', 'use_ai': False}).json()
        cancelled = post(client, accepted['status_url'] + '/cancel')
        assert cancelled.status_code == 202 and cancelled.json()['state'] == 'cancelled'
        assert client.get(accepted['status_url']).json()['result_url'] is None
