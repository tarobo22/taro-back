"""Transport/contract checks, not a score for real model empathy."""
import json

import httpx
import pytest

from api.adapters.counseling_openai import OpenAICounselingModel
from api.domain.counseling import Analysis, CounselingError


def model_returning(value):
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(value)}]}
        ]})

    model = OpenAICounselingModel(httpx.Client(transport=httpx.MockTransport(reply)),
                                'configured-model', lambda: 'test-boundary-only')
    return model, requests


def result(**changes):
    return {'empathy': '거절한 뒤에도 마음이 쓰이셨겠어요.', 'facts': [], 'tarot': [],
            'advice': '지금 마음을 편하게 들려주셔도 괜찮아요.', 'actions': [],
            'follow_up_question': None, 'safety_mode': 'standard', **changes}


def test_exploration_uses_strategy_policy_without_promoting_private_input():
    answer = Analysis(understanding='쉬는 시간이 부족하다는 말씀이군요.',
                      clarification_question=None, explicit_definitions=[], unknown_terms=[]).model_dump()
    model, requests = model_returning(answer)
    model.analyze({'text': 'PRIVATE_STUDENT_STORY', 'dictionary': []})
    request = requests[0]
    assert '<support_strategy stage="exploration"' in request['instructions']
    assert 'reflection_of_feelings' in request['instructions']
    assert 'PRIVATE_STUDENT_STORY' not in request['instructions']
    assert 'PRIVATE_STUDENT_STORY' in request['input'][0]['content']
    assert 'tools' not in request and len(requests) == 1


def test_listening_selects_reflection_instead_of_action_policy():
    model, requests = model_returning(result())
    value = model.compose({'response_mode': 'listen', 'history': [], 'cards': [], 'sources': []})
    assert value == result()
    instructions = requests[0]['instructions']
    assert '<support_strategy stage="comforting"' in instructions
    assert 'follow_up_question=null' in instructions
    assert '<support_strategy stage="action"' not in instructions


def test_confirmed_advice_selects_action_policy_and_keeps_card_context():
    model, requests = model_returning(result(actions=['오늘 할 일을 하나 줄여보세요.']))
    model.compose({'response_mode': 'advice', 'cards': [{'card_id': 7}], 'sources': [],
                   'understanding': '쉴 시간이 필요하다는 말씀이군요.'})
    assert '<support_strategy stage="action"' in requests[0]['instructions']
    assert json.loads(requests[0]['input'][0]['content'])['cards'] == [{'card_id': 7}]
    assert len(requests) == 1


@pytest.mark.parametrize('changes', [
    {'follow_up_question': '무슨 일이 있었는지 다시 설명해주실래요?'},
    {'actions': ['밖에 나가보세요.']},
    {'tarot': [{'card_id': 3, 'reflection': '새로운 시작을 생각해봐요.'}]},
    {'facts': [{'text': '공식 정보입니다.', 'source_ids': ['web:test']}]},
])
def test_listening_rejects_unrequested_questions_actions_cards_or_fact_dump(changes):
    model, requests = model_returning(result(**changes))
    with pytest.raises(CounselingError, match='AI_OUTPUT_INVALID'):
        model.compose({'response_mode': 'listen', 'cards': [], 'sources': []})
    assert len(requests) == 1


def test_advice_rejects_multiple_actions_without_automatic_extra_model_call():
    model, requests = model_returning(result(actions=['할 일을 하나 적어보세요.', '친구에게 연락해보세요.']))
    with pytest.raises(CounselingError, match='AI_OUTPUT_INVALID'):
        model.compose({'response_mode': 'advice', 'cards': [], 'sources': []})
    assert len(requests) == 1


def test_unknown_strategy_is_rejected_before_provider_call():
    model, requests = model_returning(result())
    with pytest.raises(CounselingError, match='AI_OUTPUT_INVALID'):
        model.compose({'response_mode': 'INJECT_PRIVATE_INSTRUCTION'})
    assert not requests
