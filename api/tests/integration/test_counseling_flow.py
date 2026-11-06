from copy import deepcopy
from pathlib import Path
from uuid import UUID,uuid4
import json

import pytest

from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
from api.adapters.counseling_postgres import PostgresCounselingStore
from api.application.ai_jobs import AIJobsService
from api.application.counseling import CounselingService
from api.domain.counseling import Analysis,CounselingError

pytestmark=pytest.mark.integration


class Model:
    def __init__(self):
        self.analysis=Analysis(acknowledgement='많이 버거우셨겠어요.',understanding='수업과 알바를 함께 챙기느라 쉴 시간이 부족하다는 거죠?',
            clarification_question=None,explicit_definitions=[],unknown_terms=[],perspective='학생')
        self.calls=[];self.fail=False;self.on_compose=None;self.on_analyze=None

    def available(self):return True

    def analyze(self,payload):
        self.calls.append(('analyze',deepcopy(payload)))
        if self.on_analyze:self.on_analyze()
        return self.analysis.model_dump()

    def search(self,query):
        self.calls.append(('search',query))
        if self.fail:raise CounselingError('AI_PROVIDER_ERROR',502)
        return [{'id':'web:official','kind':'web','title':'공식 안내','url':'https://www.work24.go.kr/','text':'공식 지원 제도를 확인할 수 있어요.'}]

    def compose(self,payload):
        self.calls.append(('compose',deepcopy(payload)))
        if self.on_compose:self.on_compose()
        if self.fail:raise CounselingError('AI_PROVIDER_ERROR',502)
        web=[s for s in payload['sources'] if s['kind']=='web']
        return {'empathy':'함께 챙길 일이 많아 부담되셨겠어요.',
            'facts':[{'text':'공식 안내를 확인할 수 있어요.','source_ids':[web[0]['id']]}] if web else [],
            'tarot':[{'card_id':c['card_id'],'reflection':'내가 조절할 수 있는 작은 선택을 살펴봐요.'} for c in payload['cards']],
            'advice':'지금 감당할 수 있는 만큼을 정해도 괜찮아요.',
            'actions':[] if payload['response_mode']=='listen' else ['이번 주에 줄일 수 있는 일 하나를 적어보세요.'],
            'follow_up_question':'어떤 이야기부터 더 나눠볼까요?','safety_mode':'standard'}


@pytest.fixture
def counseling(pg_pool,owners):
    root=Path(__file__).parents[3]
    with pg_pool.connection() as c:
        for p in sorted((root/'api/migrations').glob('*.sql')):c.execute(p.read_text(encoding='utf-8'))
    model=Model();store=PostgresCounselingStore(pg_pool)
    cards=json.loads((root/'shared/data-catalog.json').read_text(encoding='utf-8'))['cards']
    service=CounselingService(store,model,cards,policy={'owner_daily_limit':10000,'global_daily_limit':1000000},sample=lambda p,n:p[:n])
    consent_service=AIJobsService(lambda:PostgresAIJobsUnitOfWork(pg_pool))
    consents={o:consent_service.record_consent(o,uuid4(),'ai_processing','2026-11-05',True)['id'] for o in owners}
    return service,model,store,owners,consents,consent_service


def message(env,sid,text='학생인데 수업과 알바를 함께 챙겨야 해요.',**changes):
    service,_,_,owners,consents,_=env;owner=owners[0];v=service.get(owner,sid)
    return service.enqueue(owner,sid,changes.pop('key',uuid4()),'message',{
        'text':text,'expected_version':v['version'],'consent_event_id':consents[owner],**changes})


def confirmed(env,sid,**changes):
    service,_,_,owners,consents,_=env;owner=owners[0];v=service.get(owner,sid)
    return service.enqueue(owner,sid,changes.pop('key',uuid4()),'confirm',{
        'confirmed':True,'web_search':True,'card_count':3,'expected_version':v['version'],
        'consent_event_id':consents[owner],**changes})


def start(env):
    s,_,_,owners,_,_=env
    return UUID(s.create(owners[0],uuid4())['id'])


def test_confirmation_precedes_random_draw_and_final_advice(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    assert message(counseling,sid)['job']['state']=='queued' and not m.calls
    assert s.run_next()
    v=s.get(owners[0],sid)
    assert v['phase']=='confirmation' and v['acknowledgement'] and v['cards']==[]
    confirmed(counseling,sid);assert s.run_next()
    v=s.get(owners[0],sid)
    assert v['phase']=='advice' and [c['card_id'] for c in v['cards']]==[0,1,2]
    assert v['draw']['method']=='python.SystemRandom.sample' and v['result']['actions']


def test_owner_isolation_is_enforced_by_both_service_and_database_rls(counseling):
    s,_,store,(a,b),_,_=counseling
    sa=UUID(s.create(a,uuid4())['id']);sb=UUID(s.create(b,uuid4())['id'])
    s.define(a,'반짝','독서모임');s.define(b,'반짝','B_PRIVATE_ONLY')
    with pytest.raises(CounselingError,match='NOT_FOUND'):s.get(a,sb)
    with store.transaction(a) as r:
        rows=r.connection.execute('SELECT definition FROM personal_glossary').fetchall()
        assert rows==[{'definition':'독서모임'}]
        assert r.connection.execute('SELECT id FROM counseling_sessions WHERE id=%s',(sb,)).fetchone() is None
    with store.transaction(b) as r:
        assert r.connection.execute('SELECT definition FROM personal_glossary').fetchone()['definition']=='B_PRIVATE_ONLY'
    with store.transaction() as r:
        assert r.connection.execute('SELECT count(*) AS n FROM personal_glossary').fetchone()['n']==0


def test_twenty_repeated_requests_return_one_job_and_one_model_call(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling);key=uuid4()
    first=message(counseling,sid,key=key)
    for _ in range(19):assert message(counseling,sid,key=key)['job']['id']==first['job']['id']
    s.run_next();assert len(m.calls)==1
    confirmed(counseling,sid);s.run_next();draw=s.get(owners[0],sid)['draw']
    assert draw and not s.run_next() and len(m.calls)==2


def test_personal_definitions_persist_but_conversation_only_definitions_do_not(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    m.analysis.explicit_definitions=[{'term':'반짝','definition':'우리 독서모임','evidence_quote':'반짝은 우리 독서모임이야'}]
    # Validate the provider boundary again, just as a real Responses adapter does.
    m.analysis=Analysis.model_validate(m.analysis.model_dump())
    message(counseling,sid,'반짝은 우리 독서모임이야');s.run_next()
    assert s.glossary(owners[0])[0]['definition']=='우리 독서모임'
    m.analysis=Analysis(understanding='별명의 의미를 알려주셨군요.',clarification_question=None,
        explicit_definitions=[{'term':'초록','definition':'친구 별명','evidence_quote':'초록은 친구 별명이야'}],unknown_terms=[],memory_scope='conversation')
    message(counseling,sid,'초록은 친구 별명이야. 이번 대화에서만 기억해줘.');s.run_next()
    assert [d['term'] for d in s.glossary(owners[0])]==['반짝']


def test_unknown_personal_term_is_clarified_without_search_or_draw(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    m.analysis.unknown_terms=[{'term':'초록','kind':'personal'}];m.analysis=Analysis.model_validate(m.analysis.model_dump())
    message(counseling,sid,'초록 때문에 걱정돼요.');s.run_next()
    v=s.get(owners[0],sid)
    assert v['phase']=='clarifying' and '초록' in v['clarification_question'] and v['search_queries']==[] and v['cards']==[]
    assert [x[0] for x in m.calls]==['analyze']


def test_web_sources_are_clickable_and_model_gets_only_confirmed_cards(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    m.analysis.unknown_terms=[{'term':'국민취업지원제도','kind':'public'}];m.analysis=Analysis.model_validate(m.analysis.model_dump())
    message(counseling,sid);s.run_next();confirmed(counseling,sid);s.run_next()
    v=s.get(owners[0],sid)
    assert v['result']['facts'][0]['source_ids']==['web:official']
    assert any(d['url']=='https://www.work24.go.kr/' for d in v['sources'])
    assert m.calls[1]==('search','국민취업지원제도 공식 자료')
    assert [c['card_id'] for c in m.calls[2][1]['cards']]==[0,1,2]


def test_failed_generation_retry_keeps_the_original_draw(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    message(counseling,sid);s.run_next();m.fail=True
    confirmed(counseling,sid);s.run_next();v=s.get(owners[0],sid)
    assert v['job']['state']=='failed' and v['result'] is None
    draw=v['draw'];m.fail=False;confirmed(counseling,sid);s.run_next()
    assert s.get(owners[0],sid)['draw']==draw


def test_consent_revocation_during_provider_io_prevents_publishing(counseling):
    s,m,_,owners,_,consent_service=counseling;sid=start(counseling)
    message(counseling,sid);s.run_next()
    m.on_compose=lambda:consent_service.record_consent(owners[0],uuid4(),'ai_processing','2026-11-05',False)
    confirmed(counseling,sid);s.run_next();v=s.get(owners[0],sid)
    assert v['job']['state']=='failed' and v['job']['error_code']=='CONSENT_REQUIRED' and v['result'] is None


def test_listening_mode_respects_user_choice_and_skips_cards(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    m.analysis.response_mode='listen'
    message(counseling,sid,'오늘은 조언 말고 내 이야기만 들어줘.');s.run_next();confirmed(counseling,sid);s.run_next()
    v=s.get(owners[0],sid)
    assert v['phase']=='advice' and not v['cards'] and v['result']['actions']==[]


def test_urgent_safety_message_bypasses_model_and_cards(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    message(counseling,sid,'지금 내 몸을 해칠까 봐 무서워요.');s.run_next();v=s.get(owners[0],sid)
    assert v['phase']=='support' and not v['cards'] and not m.calls


def test_forgetting_definition_invalidates_old_retrieval_context(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    d=s.define(owners[0],'반짝','우리 독서모임')
    message(counseling,sid,'반짝에서 발표를 맡았어요.');s.run_next()
    assert s.get(owners[0],sid)['glossary_used']
    s.forget(owners[0],UUID(d['id']))
    v=s.get(owners[0],sid)
    assert not s.glossary(owners[0]) and v['phase']=='listening' and not v['glossary_used']


def test_deleting_session_during_model_io_discards_result(counseling):
    s,m,_,owners,_,_=counseling;sid=start(counseling)
    m.on_analyze=lambda:s.delete(owners[0],sid)
    message(counseling,sid);assert s.run_next()
    with pytest.raises(CounselingError,match='NOT_FOUND'):s.get(owners[0],sid)
