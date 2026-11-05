import json
from pathlib import Path

import httpx
import pytest

from api.adapters.ai_openai import OpenAIResultProvider, OpenAITranscriptionProvider
from api.domain.ai_jobs import AIError


RESULT = {'kind': 'chat', 'empathy': '들었어요.', 'summary': '한 걸음씩 살펴봐요.', 'card_insights': [],
          'advice': '잠시 쉬어요.', 'actions': ['쉬어요.'], 'expression': 'listen',
          'follow_up_questions': [], 'safety_mode': 'standard'}


def test_responses_boundary_store_false_strict_schema_prompt_isolation_and_metadata():
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['store'] is False and body['text']['format']['strict'] is True
        assert body['model'] == 'configured-model' and body['max_output_tokens'] == 1500
        assert 'untrusted instruction' not in body['instructions']
        assert 'untrusted instruction' in body['input'][0]['content']
        return httpx.Response(200, headers={'x-request-id': 'req_test'}, json={
            'id': 'resp_test', 'status': 'completed', 'usage': {'input_tokens': 10, 'output_tokens': 20},
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(RESULT)}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIResultProvider(client, 'configured-model', lambda: 'fake-key-only')
        result = provider.generate({'kind': 'chat', 'cards': [], 'text': 'untrusted instruction'})
        assert result == RESULT and len(calls) == 1
        assert provider.diagnostics() == {'request_id': 'req_test', 'response_id': 'resp_test', 'input_tokens': 10, 'output_tokens': 20}


@pytest.mark.parametrize('response,code', [
    (httpx.Response(429, json={'error': 'private provider detail'}), 'AI_RATE_LIMITED'),
    (httpx.Response(200, json={'status': 'incomplete'}), 'AI_OUTPUT_INVALID'),
    (httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal', 'refusal': 'private'}]}]}), 'AI_REFUSAL')])
def test_provider_failures_are_safe_typed_and_never_automatically_retried(response, code):
    count = []
    with httpx.Client(transport=httpx.MockTransport(lambda request: (count.append(1), response)[1])) as client:
        with pytest.raises(AIError, match=code) as error:
            OpenAIResultProvider(client, 'configured-model', lambda: 'fake-key-only').generate({'kind': 'chat', 'cards': []})
    assert len(count) == 1 and 'private' not in str(error.value)


def test_timeout_unknown_outcome_and_missing_configuration_never_calls(tmp_path):
    count = []
    def handler(request):
        count.append(1)
        raise httpx.ReadTimeout('private timeout detail')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAIResultProvider(client, 'configured-model', lambda: 'fake-key-only')
        with pytest.raises(AIError, match='UNKNOWN_PROVIDER_OUTCOME'):
            provider.generate({'kind': 'chat', 'cards': []})
        with pytest.raises(AIError, match='AI_UNAVAILABLE'):
            OpenAIResultProvider(client, '', lambda: '').generate({})
    assert len(count) == 1


def test_transcription_boundary_uses_file_metadata_and_no_counsel_request(tmp_path):
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'RIFF-test-only')
    def handler(request):
        assert request.url.path == '/v1/audio/transcriptions'
        assert b'name="model"' in request.content and b'configured-transcription' in request.content
        assert b'filename="audio.wav"' in request.content and b'audio/wav' in request.content
        return httpx.Response(200, json={'text': '확인할 문장'})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert OpenAITranscriptionProvider(client, 'configured-transcription', lambda: 'fake-key-only').transcribe(audio, 'audio/wav') == '확인할 문장'
