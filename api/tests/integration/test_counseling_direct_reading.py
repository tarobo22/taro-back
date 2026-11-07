from uuid import uuid4

import pytest

from api.domain.counseling import Analysis
from api.tests.integration.test_counseling_flow import counseling, start, message, confirmed
from api.tests.integration.test_counseling_http import client


@pytest.mark.parametrize('count', [1, 3, 5])
def test_clear_topic_opens_selected_cards_in_the_same_job(counseling, count):
    service, model, _, owners, _, _ = counseling
    sid, key = start(counseling), uuid4()
    queued = message(counseling, sid, '취업운', key=key, auto_read=True, card_count=count, web_search=False)
    assert queued['cards'] == [] and not model.calls
    def verify_saved_draw():
        saved = service.get(owners[0], sid)
        assert len(saved['cards']) == count and saved['draw']
        assert saved['history'][0]['text'] == '취업운'
    model.on_compose = verify_saved_draw
    assert service.run_next()
    result = service.get(owners[0], sid)
    assert result['job']['state'] == 'succeeded'
    assert result['phase'] == 'advice' and len(result['cards']) == count
    assert [call[0] for call in model.calls] == ['analyze', 'compose']
    again = service.enqueue(owners[0], sid, key, 'message', dict(text='취업운', expected_version=1,
        consent_event_id=counseling[4][owners[0]], auto_read=True, card_count=count, web_search=False))
    assert again['job']['id'] == queued['job']['id'] and not service.run_next()


def test_ambiguous_topic_stops_for_one_question_without_drawing(counseling):
    service, model, _, owners, _, _ = counseling
    model.analysis = Analysis(acknowledgement='학교에서 많이 지치셨군요.', understanding='학교생활이 힘드신 상황이에요.',
        clarification_question='공부나 시험, 친구 관계 중 어떤 일 때문에 힘드신가요?', explicit_definitions=[], unknown_terms=[])
    sid = start(counseling)
    message(counseling, sid, '요즘 학교가 힘들어', auto_read=True, card_count=3)
    service.run_next()
    result = service.get(owners[0], sid)
    assert result['phase'] == 'clarifying' and not result['cards'] and result['result'] is None
    assert result['clarification_question'] == model.analysis.clarification_question
    assert [call[0] for call in model.calls] == ['analyze']
    model.analysis.clarification_question = None
    message(counseling, sid, '시험 때문이야', auto_read=True, card_count=3)
    service.run_next()
    assert service.get(owners[0], sid)['phase'] == 'advice'


def test_direct_reading_failure_keeps_draw_for_explicit_retry(counseling):
    service, model, _, owners, _, _ = counseling
    sid = start(counseling)
    message(counseling, sid, '시험 때문에 힘들어요', auto_read=True, card_count=5)
    model.fail = True
    service.run_next()
    failed = service.get(owners[0], sid)
    assert failed['job']['state'] == 'failed' and len(failed['cards']) == 5
    assert failed['phase'] == 'confirmation'
    model.fail = False
    confirmed(counseling, sid, card_count=5)
    service.run_next()
    assert service.get(owners[0], sid)['draw'] == failed['draw']


def test_direct_reading_saves_explicit_meanings_before_reply(counseling):
    service, model, _, owners, _, _ = counseling
    model.analysis = Analysis(understanding='독서모임에서 관계가 고민이시군요.', clarification_question=None,
        explicit_definitions=[{'term': '반짝', 'definition': '우리 독서모임', 'evidence_quote': '반짝은 우리 독서모임이야'}], unknown_terms=[])
    sid = start(counseling)
    message(counseling, sid, '반짝은 우리 독서모임이야. 반짝 친구와 관계가 고민이야.', auto_read=True, card_count=3)
    service.run_next()
    result = service.get(owners[0], sid)
    assert result['job']['state'] == 'succeeded'
    payload = next(payload for kind, payload in model.calls if kind == 'compose')
    assert any(source['kind'] == 'user_definition' and source['text'] == '우리 독서모임' for source in payload['sources'])


def test_http_accepts_direct_reading_and_rejects_invalid_card_count(counseling):
    owner = counseling[3][0]
    http = client(counseling, owner)
    sid = start(counseling)
    body = dict(text='취업', expected_version=1, consent_event_id=counseling[4][owner], auto_read=True, card_count=5)
    path = '/v2/counseling/sessions/' + str(sid) + '/messages'
    assert http.post(path, json={**body, 'card_count': 2}, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 422
    assert http.post(path, json=body, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 202
    counseling[0].run_next()
    result = http.get('/v2/counseling/sessions/' + str(sid)).json()
    assert result['phase'] == 'advice' and len(result['cards']) == 5
