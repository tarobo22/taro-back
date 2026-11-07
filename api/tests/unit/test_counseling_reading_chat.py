from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid4

import pytest

from api.application.counseling import CounselingService, memory_revision
from api.domain.counseling import Analysis, CounselingError, draw_cards, grounded_result


def cards(count=5):
    return draw_cards([{'id': i, 'name_ko': str(i), 'basic_meaning': '마음을 돌아보는 관점이에요.'} for i in range(78)], count, sample=lambda items, n: items[:n])


def reply(selected):
    return dict(empathy='마음이 많이 쓰이셨겠어요.', facts=[],
        tarot=[dict(card_id=c['card_id'], reflection='작게 시작해도 괜찮아요.') for c in selected],
        advice='지금 가장 마음에 걸리는 일을 함께 살펴봐요.', actions=['원하는 도움을 한 가지 적어보세요.'],
        follow_up_question=None, safety_mode='standard')


def test_five_card_reply_preserves_all_five_server_cards_in_order():
    selected=cards()
    assert len({c['card_id'] for c in selected}) == 5
    assert [c['position'] for c in selected] == [1, 2, 3, 4, 5]
    assert len({c['position_label'] for c in selected}) == 5
    assert grounded_result(reply(selected), selected, [])['tarot'] == reply(selected)['tarot']
    wrong=reply(selected)
    wrong['tarot'].reverse()
    with pytest.raises(CounselingError, match='OUTPUT_INVALID'):
        grounded_result(wrong, selected, [])


def test_followup_preserves_draw_and_passes_counseling_first_context():
    selected=cards(3)
    class Model:
        payload=None
        def analyze(self, payload):
            return Analysis(acknowledgement='그 뒤에도 마음이 쓰이셨군요.', understanding='지원서를 쓰는 일이 막막하다는 말씀이군요?', clarification_question=None, explicit_definitions=[], unknown_terms=[]).model_dump()
        def compose(self, payload):
            self.payload=payload
            return reply(payload['cards'])
    model=Model()
    service=CounselingService(None, model, [])
    service._guard=lambda *args: None
    previous=dict(cards=selected, draw={'id': 'kept-draw'}, has_prior_advice=True,
                  history=[{'role': 'user', 'text': '취업 준비 때문에 걱정돼요.'}])
    state,_=service._analyze({'payload': {'text': '지원서를 쓰기가 막막해요.'}}, previous, [], memory_revision([]))
    assert state['cards'] == selected and state['draw'] == previous['draw']
    state=service._reply({'payload': {}}, state, [], memory_revision([]))
    assert state['cards'] == selected
    assert model.payload['follow_up'] is True
    assert model.payload['history'][-2]['text'] == '지원서를 쓰기가 막막해요.'


def test_continue_reading_uses_owned_source_and_reuses_session():
    owner,source,key=uuid4(),uuid4(),uuid4()
    selected=cards(3)
    class Repo:
        row=None
        def session_for_key(self, owner, key): return self.row
        def create_session(self, owner, sid, topic, state, key):
            self.row=dict(id=sid, owner_id=owner, topic=topic, state=deepcopy(state), version=1)
            return self.row
        def latest_command(self, owner, sid): return None
    repo=Repo()
    class Store:
        @contextmanager
        def transaction(self, owner): yield repo
    calls=[]
    def lookup(actual_owner, actual_source):
        calls.append((actual_owner, actual_source))
        return dict(question='취업 준비가 막막해요.', selected_cards=selected,
                    result={'advice': '한 가지부터 정리해 봐요.'}, created_at='2026-11-07T00:00:00Z')
    service=CounselingService(Store(), None, [], reading_source=lookup)
    first=service.create(owner,key,'work',source)
    again=service.create(owner,key,'work',source)
    assert first['id'] == again['id']
    assert first['cards'] == selected and first['draw']['method'] == 'saved_reading'
    assert first['history'][0]['text'] == '취업 준비가 막막해요.'
    assert calls and all(o == owner and s == source for o,s in calls)
    with pytest.raises(CounselingError,match='IDEMPOTENCY_CONFLICT'):
        service.create(owner,key,'work',uuid4())


def test_followup_rejects_card_explanations_longer_than_the_short_contract():
    import json
    import httpx
    from api.adapters.counseling_openai import OpenAICounselingModel
    value=reply(cards(3))
    value['tarot'][0]['reflection']='가' * 61
    def handler(request):
        return httpx.Response(200,json={'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(value)}]}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model=OpenAICounselingModel(client,'configured-model',lambda:'test-boundary-only')
        with pytest.raises(CounselingError,match='AI_OUTPUT_INVALID'):
            model.compose({'follow_up':True,'response_mode':'advice','cards':cards(3)})
