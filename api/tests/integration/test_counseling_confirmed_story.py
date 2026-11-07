import pytest

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
