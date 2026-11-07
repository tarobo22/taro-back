import json
from copy import deepcopy
from uuid import uuid4

import httpx
import pytest

from api.adapters.counseling_openai import OpenAICounselingModel
from api.domain.counseling import CounselingError
from api.tests.integration.test_counseling_flow import counseling, confirmed, message, start


pytestmark = pytest.mark.integration


def stored_story(env, session_id):
    _, _, store, (owner, _), _, _ = env
    with store.transaction(owner) as repo:
        return repo.session(owner, session_id)['state'].get('confirmed_story')


def test_confirmation_saves_latest_user_evidence_and_separates_glossary(counseling):
    service, model, _, (owner, _), _, _ = counseling
    session = start(counseling)
    first = '오름에서 이번 주에 제가 발표하기로 했어요.'
    correction = '정정할게요. 다음 달에 선배가 발표하고 저는 자료만 나눠줘요.'
    service.define(owner, '오름', '주말 등산 모임')
    message(counseling, session, first)
    assert service.run_next()
    received = []

    def analyze(payload):
        received.append(payload)
        return {**model.analysis.model_dump(),
                'understanding': '다음 달에 선배가 발표하고 자료만 나눠주는 역할이라는 거죠?',
                'story_anchors': [
                    {'slot': 'time', 'turn': 1, 'quote': '이번 주에'},
                    {'slot': 'time', 'turn': 0, 'quote': '다음 달에'},
                    {'slot': 'actor', 'turn': 0, 'quote': '선배가 발표하고 저는 자료만 나눠줘요'},
                    {'slot': 'event', 'turn': 0, 'quote': '주말 등산 모임'},
                ]}

    model.analyze = analyze
    model.review_reply = lambda payload, reply, reasons: reply
    message(counseling, session, correction)
    assert service.run_next()
    assert received[0].get('user_turns') == [correction, first]
    assert service.get(owner, session)['job']['state'] == 'succeeded'
    assert service.get(owner, session)['phase'] == 'confirmation'
    assert stored_story(counseling, session) is None

    confirmed(counseling, session)
    assert service.run_next()
    story = stored_story(counseling, session)
    assert story['latest_message'] == correction
    assert story['mode'] == 'advice'
    assert {a['slot']: a['quote'] for a in story['anchors']} == {
        'time': '다음 달에', 'actor': '선배가 발표하고 저는 자료만 나눠줘요',
    }
    assert model.calls[-1][1]['confirmed_story'] == story
    public = service.get(owner, session)
    assert 'confirmed_story' not in public and public['result']['actions']


@pytest.mark.parametrize('repair', [True, False])
def test_review_checks_story_and_never_publishes_an_unrepaired_task(counseling, repair):
    service, fixture_model, _, (owner, _), _, _ = counseling
    session = start(counseling)
    story_text = '주말 약속을 취소하고 쉬고 싶어요. 상대가 실망할까 걱정돼요.'
    analysis = {**fixture_model.analysis.model_dump(),
                'understanding': '주말 약속을 취소하고 싶은데 상대가 실망할까 걱정되는 거죠?',
                'story_anchors': [{'slot': 'time', 'turn': 0, 'quote': '주말 약속'}]}
    draft = {
        'empathy': '쉬고 싶은 마음과 상대가 실망할까 걱정되는 마음이 함께 있군요.',
        'advice': '오늘 약속을 취소하면 쉴 수 있어요.',
        'actions': ['쉬고 싶다는 마음을 전해보기.'],
        'facts': [], 'follow_up_question': None, 'safety_mode': 'standard',
        'tarot': [{'card_id': index, 'reflection': '시간을 나눠 기록해 보세요.'} for index in range(3)],
    }
    fixed = {**draft, 'advice': '쉴 시간이 필요하다는 말만으로도 이유는 충분해요.',
             'tarot': [{'card_id': index, 'reflection': '나에게 필요한 여유도 살펴볼 수 있는 카드예요.'}
                       for index in range(3)]}
    captured = []
    outputs = [analysis, draft, {'reply': fixed if repair else draft}]

    def respond(request):
        captured.append(json.loads(request.content))
        result = outputs[len(captured) - 1]
        return httpx.Response(200, json={'status': 'completed', 'output': [
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(result)}]},
        ]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        service.model = OpenAICounselingModel(client, 'test-model', lambda: 'test-boundary-only')
        message(counseling, session, story_text)
        assert service.run_next()
        confirmed(counseling, session)
        assert service.run_next()

    assert [call['text']['format']['name'] for call in captured] == [
        'StoryAnalysis', 'CounselResult', 'ReviewedReply',
    ]
    review = json.loads(captured[-1]['input'][0]['content'])
    assert review['confirmed_story']['latest_message'] == story_text
    assert [card['card_id'] for card in review['cards']] == [0, 1, 2]
    assert {'extra_instruction', 'unanchored_time'} <= set(review['review_reasons'])
    assert 'tools' not in captured[-1] and captured[-1]['store'] is False
    public = service.get(owner, session)
    if repair:
        assert public['job']['state'] == 'succeeded' and public['result'] == fixed
    else:
        assert public['job']['error_code'] == 'OUTPUT_INVALID' and public['result'] is None
        assert [card['card_id'] for card in public['cards']] == [0, 1, 2]


def test_revoked_consent_stops_review_before_provider_call(counseling):
    service, model, _, (owner, _), _, consent_service = counseling
    session = start(counseling)
    message(counseling, session)
    assert service.run_next()
    compose = model.compose
    review_calls = []

    def revoke_and_reply(payload):
        result = deepcopy(compose(payload))
        result['advice'] = '하루 계획부터 적어 보세요.'
        consent_service.record_consent(owner, uuid4(), 'ai_processing', '2026-11-05', False)
        return result

    model.compose = revoke_and_reply
    model.review_reply = lambda *args: review_calls.append(args)
    confirmed(counseling, session)
    assert service.run_next()
    public = service.get(owner, session)
    assert not review_calls and public['result'] is None
    assert public['job']['error_code'] == 'CONSENT_REQUIRED'


def test_listening_takes_precedence_over_optional_clarification(counseling):
    service, model, _, (owner, _), _, _ = counseling
    session = start(counseling)
    model.analysis.response_mode = 'listen'
    model.analysis.clarification_question = '어떤 일이었는지 더 설명해주실래요?'
    message(counseling, session, '설명할 힘이 없어요. 지금은 조언 없이 그냥 들어주세요.')
    assert service.run_next()
    draft = service.get(owner, session)
    assert draft['phase'] == 'confirmation'
    assert draft['clarification_question'] is None
    assert model.analysis.clarification_question not in draft['history'][-1]['text']

    confirmed(counseling, session)
    assert service.run_next()
    public = service.get(owner, session)
    assert stored_story(counseling, session)['mode'] == 'listen'
    assert public['result']['follow_up_question'] is None
    assert not public['result']['actions'] and not public['cards']
    assert [call[0] for call in model.calls] == ['analyze', 'compose']


def test_question_route_waits_for_an_answer_before_confirming(counseling):
    service, model, _, (owner, _), _, _ = counseling
    session = start(counseling)
    model.analysis.clarification_question = '어떤 일이 있었는지 조금 들려주실래요?'
    message(counseling, session, '그 일 때문에 마음이 복잡해요.')
    assert service.run_next()
    public = service.get(owner, session)
    assert public['phase'] == 'clarifying' and not public['cards']
    assert stored_story(counseling, session) is None
    with pytest.raises(CounselingError, match='CONFIRMATION_REQUIRED'):
        confirmed(counseling, session)


def test_retry_uses_same_confirmed_story_and_draw(counseling):
    service, model, _, (owner, _), _, _ = counseling
    session = start(counseling)
    message(counseling, session)
    assert service.run_next()
    model.fail = True
    confirmed(counseling, session)
    assert service.run_next()
    story = stored_story(counseling, session)
    draw = service.get(owner, session)['draw']
    assert story and draw

    model.fail = False
    confirmed(counseling, session)
    assert service.run_next()
    assert stored_story(counseling, session) == story
    assert service.get(owner, session)['draw'] == draw
    assert model.calls[-1][1]['confirmed_story'] == story
