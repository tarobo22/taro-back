import io
import wave
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.ai_audio import PrivateAudioStore
from api.adapters.http.ai_jobs import create_ai_jobs_router
from api.application.ai_jobs import AIJobsService
from api.domain.ai_jobs import AIError
from api.tests.integration.test_p0_ai_jobs import Provider, ai_env, consent

pytestmark = pytest.mark.integration


def wav(seconds=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(8000)
        stream.writeframes(b'\0\0' * int(8000 * seconds))
    return out.getvalue()


class Transcriber:
    model = 'fake-transcriber'
    calls = 0

    def available(self):
        return True

    def transcribe(self, path, mime_type):
        assert Path(path).is_file()
        assert mime_type == 'audio/wav'
        self.calls += 1
        return '편집하고 확인한 뒤 보낼 문장'


def test_http_exact_dtos_idempotency_owner_errors_and_status_only_sse(ai_env):
    service, provider, _, owners, _ = ai_env
    current = [owners[0]]
    app = FastAPI()
    app.include_router(create_ai_jobs_router(service, lambda: current[0]))
    with TestClient(app) as client:
        assert client.post('/v2/conversations', json={'topic': 'care', 'tone': 'warm'}).status_code == 422
        headers = {'X-Idempotency-Key': str(uuid4())}
        c = client.post('/v2/conversations', headers=headers, json={'topic': 'care', 'tone': 'warm'})
        assert c.status_code == 201
        assert set(c.json()) == {'id', 'topic', 'tone', 'state', 'created_at', 'updated_at'}
        cid = c.json()['id']
        ce = client.post('/v2/me/consents', headers={'X-Idempotency-Key': str(uuid4())},
                         json={'kind': 'ai_processing', 'policy_version': '2026-11-05', 'granted': True})
        assert ce.status_code == 201
        body = {'client_message_id': str(uuid4()), 'input_source': 'choice', 'answers': [
            {'question_id': 'control', 'answer': False}], 'consent_event_id': ce.json()['id']}
        a = client.post(f'/v2/conversations/{cid}/turns', headers={'X-Idempotency-Key': str(uuid4())}, json=body)
        assert a.status_code == 202
        assert set(a.json()) == {'job_id', 'resource_id', 'state', 'status_url', 'events_url'}
        assert not provider.calls
        current[0] = owners[1]
        assert client.get(a.json()['status_url']).status_code == 404
        current[0] = owners[0]
        assert service.run_next('http-worker')
        stream = client.get(a.json()['events_url'])
        assert stream.status_code == 200 and 'event: completed' in stream.text
        assert 'assistant_result' not in stream.text and '고마워요' not in stream.text
        assert len(provider.calls) == 1


def test_transcription_separate_consent_private_file_cleanup_and_no_counsel_submission(ai_env, tmp_path):
    _, _, pool, owners, factory = ai_env
    transcriber = Transcriber()
    store = PrivateAudioStore(tmp_path / 'audio')
    service = AIJobsService(factory, transcription_provider=transcriber, audio_store=store)
    owner = owners[0]
    with pytest.raises(AIError, match='CONSENT_REQUIRED'):
        service.create_transcription(owner, uuid4(), wav(), 'audio/wav', 1000, uuid4())
    cid = consent(service, owner, 'cloud_transcription')
    key = uuid4()
    a = service.create_transcription(owner, key, wav(), 'audio/wav', 1000, cid)
    assert service.create_transcription(owner, key, wav(), 'audio/wav', 1000, cid) == a
    assert len(list((tmp_path / 'audio').iterdir())) == 1
    assert service.run_next('audio-worker')
    assert list((tmp_path / 'audio').iterdir()) == []
    tid = UUID(a['resource_id'])
    assert service.get_transcription(owner, tid)['transcript'] == '편집하고 확인한 뒤 보낼 문장'
    assert service.list_conversations(owner)['items'] == [] and transcriber.calls == 1
    with pool.connection() as c:
        c.execute("UPDATE p0_ai_transcriptions SET expires_at=now()-interval '1 second' WHERE id=%s", (tid,))
    with pytest.raises(AIError, match='TRANSCRIPTION_EXPIRED'):
        service.get_transcription(owner, tid)
    service.purge_expired()
    with pool.connection() as c:
        row = c.execute('SELECT transcript,audio_path FROM p0_ai_transcriptions WHERE id=%s', (tid,)).fetchone()
        assert row == {'transcript': None, 'audio_path': None}


def test_audio_rejects_spoofed_duration_type_and_over_limit_before_provider(ai_env, tmp_path):
    _, _, _, owners, factory = ai_env
    transcriber = Transcriber()
    store = PrivateAudioStore(tmp_path / 'audio')
    service = AIJobsService(factory, transcription_provider=transcriber, audio_store=store)
    owner = owners[0]
    cid = consent(service, owner, 'cloud_transcription')
    for data, mime, duration, code in [(wav(61), 'audio/wav', 1000, 'AUDIO_TOO_LONG'),
                                      (b'fake', 'audio/wav', 1000, 'AUDIO_FORMAT_UNSUPPORTED'),
                                      (wav(), 'text/plain', 1000, 'AUDIO_FORMAT_UNSUPPORTED'),
                                      (b'a' * (10485760 + 1), 'audio/wav', 1000, 'AUDIO_TOO_LARGE')]:
        with pytest.raises(AIError, match=code):
            service.create_transcription(owner, uuid4(), data, mime, duration, cid)
    assert transcriber.calls == 0 and list((tmp_path / 'audio').iterdir()) == []


def test_multipart_upload_delete_cancel_and_erasure_remove_audio(ai_env, tmp_path):
    _, _, pool, owners, factory = ai_env
    store = PrivateAudioStore(tmp_path / 'audio')
    service = AIJobsService(factory, transcription_provider=Transcriber(), audio_store=store)
    owner = owners[0]
    cid = consent(service, owner, 'cloud_transcription')
    app = FastAPI()
    app.include_router(create_ai_jobs_router(service, lambda: owner))
    with TestClient(app) as client:
        a = client.post('/v2/transcriptions', headers={'X-Idempotency-Key': str(uuid4())},
                        files={'audio': ('user-name.wav', wav(), 'audio/wav')},
                        data={'duration_ms': '1000', 'consent_event_id': cid})
        assert a.status_code == 202
        rid = a.json()['resource_id']
        assert all('user-name' not in p.name for p in (tmp_path / 'audio').iterdir())
        assert client.delete(f'/v2/transcriptions/{rid}').status_code == 204
        assert list((tmp_path / 'audio').iterdir()) == []
        assert service.get_job(owner, UUID(a.json()['job_id']))['state'] == 'cancelled'
    service.create_transcription(owner, uuid4(), wav(), 'audio/wav', 1000, cid)
    with pool.connection() as c:
        service.erase_owner(owner, connection=c)
    assert list((tmp_path / 'audio').iterdir()) == []
