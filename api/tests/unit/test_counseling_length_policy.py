import json

import httpx
import pytest

from api.adapters.counseling_openai import OpenAICounselingModel
from api.domain.counseling import CounselResult, CounselingError, draw_cards, grounded_result


def one_card():
    catalog = [{'id': i, 'name_ko': str(i), 'basic_meaning': '마음을 돌아보는 카드'} for i in range(78)]
    return draw_cards(catalog, 1, sample=lambda items, count: items[:count])


def reply(cards, field=None, additional=0):
    return {
        'empathy': '마음이 쓰이셨겠어요.',
        'facts': [],
        'tarot': [{'card_id': cards[0]['card_id'], 'reflection': '가' * (45 + (additional if field == 'tarot' else 0))}],
        'advice': '나' * (100 + (additional if field == 'advice' else 0)),
        'actions': ['다' * (45 + (additional if field == 'actions' else 0))],
        'follow_up_question': None,
        'safety_mode': 'standard',
    }


@pytest.mark.parametrize('field', ['tarot', 'advice', 'actions'])
def test_publish_rejects_oversized_new_reply_but_keeps_archived_reply_readable(field):
    cards = one_card()
    at_limit = reply(cards)
    assert grounded_result(at_limit, cards, []) == at_limit
    above_limit = reply(cards, field, additional=1)
    assert CounselResult.model_validate(above_limit).model_dump() == above_limit
    with pytest.raises(CounselingError, match='OUTPUT_INVALID'):
        grounded_result(above_limit, cards, [])


@pytest.mark.parametrize('follow_up', [False, True])
def test_openai_reply_schema_agrees_with_prompt_length_limits(follow_up):
    requests = []
    cards = one_card()

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(reply(cards))}]},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICounselingModel(client, 'configured-model', lambda: 'test-boundary-only')
        assert model.compose({'cards': cards, 'response_mode': 'advice', 'follow_up': follow_up}) == reply(cards)

    request = requests[0]
    schema = request['text']['format']['schema']
    props = schema['properties']
    reflection_ref = props['tarot']['items']['$ref'].split('/')[-1]
    reflection = schema['$defs'][reflection_ref]['properties']['reflection']
    assert reflection['maxLength'] == props['actions']['items']['maxLength'] == 45
    assert props['advice']['maxLength'] == 100
    assert '45자 이내' in request['instructions']
    assert '100자 이내' in request['instructions']
