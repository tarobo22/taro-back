"""The reply must receive the original user evidence, not only an AI paraphrase.

These are context-delivery regressions; real model correctness is checked separately.
"""
import pytest
import httpx
import json

from api.application.counseling import CounselingService, memory_revision
from api.adapters.counseling_openai import OpenAICounselingModel
from api.domain.counseling import Analysis, draw_cards


@pytest.mark.parametrize('text', [
    '먹고살기넘힘들다 쓰리잡뛰어야해',
    '지금 쓰리잡 뛰고 있어',
    '작년에는 쓰리잡 뛰었는데 지금은 하나만 해',
    '쓰리잡은 아직 안 뛰어. 다음 달부터 해야 할 것 같아',
    '동생이 쓰리잡을 뛰어야 한대. 나는 일이 하나야',
    '월세가 오르면 쓰리잡을 뛰어야 할까 봐 걱정돼',
])
def test_reply_preserves_latest_user_words_despite_wrong_ai_summary(text):
    class Model:
        payload = None

        def compose(self, payload):
            self.payload = payload
            return dict(empathy='마음이 쓰이시는군요.', facts=[], tarot=[],
                        advice='편하게 이야기해 주세요.', actions=[],
                        follow_up_question=None, safety_mode='standard')

    model = Model()
    service = CounselingService(None, model, [])
    service._guard = lambda *args: None
    history = [dict(role='assistant', text='이미 세 가지 일을 하고 계시군요.'),
               dict(role='user', text=text),
               dict(role='assistant', text='제가 이해한 내용이 맞을까요?')]
    state = dict(analysis=dict(understanding='이미 세 가지 일을 병행하고 있다.', response_mode='listen'), history=history)
    # Confirmation jobs have no text. The original message still has to reach composition.
    reply = service._reply({'payload': {}}, state, [], memory_revision([]))
    assert model.payload['latest_user_text'] == text
    assert model.payload['history'] == history
    assert reply['history'][:-1] == history


def test_user_evidence_survives_bounded_history_without_using_assistant_as_source():
    class Model:
        payload = None

        def compose(self, payload):
            self.payload = payload
            return dict(empathy='그 부담을 함께 살펴봐요.', facts=[], tarot=[],
                        advice='편하게 이야기해 주세요.', actions=[],
                        follow_up_question=None, safety_mode='standard')

    model = Model()
    service = CounselingService(None, model, [])
    service._guard = lambda *args: None
    original = '아직 시작한 게 아니라 다음 달부터 해야 할 것 같아.'
    history = [dict(role='user', text=original)] + [dict(role='assistant', text='이전 답변') for _ in range(11)]
    state = dict(analysis=dict(understanding='일의 부담', response_mode='listen'), history=history)
    service._reply({'payload': {}}, state, [], memory_revision([]))
    assert model.payload['latest_user_text'] == original
    assert len(model.payload['history']) == 10
    assert all(item['role'] == 'assistant' for item in model.payload['history'])


@pytest.mark.parametrize('text,quote,expected', [
    ('요즘 시험 때문에 힘들어요', None, 'advice'),
    ('동생이 쓰리잡을 뛰어야 한대. 동생이 걱정돼', '조언 없이 들어 주세요', 'advice'),
    ('쓰리잡을 해야 해. 오늘은 조언 말고 들어만 줘', '조언 말고 들어만 줘', 'listen'),
])
def test_listening_needs_an_actual_user_request_not_an_inferred_emotion(text, quote, expected):
    answer = Analysis(understanding='부담을 느끼신다.', response_mode='listen',
                      clarification_question=None, explicit_definitions=[], unknown_terms=[]).model_dump()
    answer['listening_request_quote'] = quote
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(answer)}]}
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = OpenAICounselingModel(client, 'configured-model', lambda: 'test-boundary-only')
        result = model.analyze({'text': text, 'history': [], 'dictionary': []})
    assert result['response_mode'] == expected
    assert 'listening_request_quote' not in result
    assert len(requests) == 1


def test_composition_cannot_rewrite_the_initial_acknowledgement_as_an_existing_event():
    selected = draw_cards([dict(id=i, name_ko=str(i), basic_meaning='생활의 균형') for i in range(78)], 1,
                          sample=lambda items, count: items[:count])
    acknowledgement = '동생이 쓰리잡을 해야 한다니 걱정되시겠어요.'

    class Model:
        def compose(self, payload):
            return dict(empathy='동생이 쓰리잡을 하고 있다니 걱정되시겠어요.', facts=[],
                        tarot=[dict(card_id=selected[0]['card_id'], reflection='감당할 수 있는 범위를 살펴봐요.')],
                        advice='동생에게 어떤 도움이 필요한지 물어봐도 좋아요.',
                        actions=['동생에게 안부 문자 보내기'], follow_up_question=None, safety_mode='standard')

    service = CounselingService(None, Model(), [])
    service._guard = lambda *args: None
    state = dict(analysis=dict(acknowledgement=acknowledgement, understanding='동생이 쓰리잡을 해야 한다.',
                               response_mode='advice'), cards=selected,
                 history=[dict(role='user', text='동생이 쓰리잡을 뛰어야 한대. 걱정돼')])
    result = service._reply({'payload': {}}, state, [], memory_revision([]))
    assert result['result']['empathy'] == acknowledgement
    assert result['result']['tarot'][0]['card_id'] == selected[0]['card_id']
    assert acknowledgement in result['history'][-1]['text']
    assert '하고 있다니' not in result['history'][-1]['text']
