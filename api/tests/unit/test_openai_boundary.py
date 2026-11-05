import json

import httpx
import pytest

from api.adapters.openai_guidance import OpenAIGuidance
from api.application.experience import GuidanceUnavailable

ADVICE = {'empathy': '꺼내 줘서 고마워요.', 'advice': '작은 것부터 살펴봐요.',
          'action': '한 문장만 적어 보세요.', 'expression': 'listen'}


def test_openai_uses_schema_and_no_storage_and_validates_response():
    def handler(request):
        body = json.loads(request.content)
        assert body['store'] is False
        assert body['text']['format']['strict'] is True
        assert body['model'] == 'configured-model'
        assert request.headers['authorization'] == 'Bearer test-only'
        return httpx.Response(200, json={'status': 'completed', 'output': [{'type': 'message',
            'content': [{'type': 'output_text', 'text': json.dumps(ADVICE)}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        guide = OpenAIGuidance(client, 'configured-model', lambda: 'test-only')
        assert guide.advise('care', 'hello', [], 'warm', []) == ADVICE


@pytest.mark.parametrize('response', [httpx.Response(429, json={'error': 'provider private detail'}),
    httpx.Response(200, json={'status': 'incomplete'}),
    httpx.Response(200, json={'status': 'completed', 'output': []})])
def test_provider_errors_never_become_success_or_expose_details(response):
    with httpx.Client(transport=httpx.MockTransport(lambda request: response)) as client:
        with pytest.raises(GuidanceUnavailable) as error:
            OpenAIGuidance(client, 'configured-model', lambda: 'test-only').advise('care', 'private', [], 'warm', [])
        assert 'private detail' not in str(error.value)
