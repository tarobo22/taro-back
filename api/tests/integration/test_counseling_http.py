from uuid import UUID,uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.counseling import create_counseling_router
from api.tests.integration.test_counseling_flow import counseling


def client(env,owner):
    app=FastAPI();app.include_router(create_counseling_router(env[0],lambda:owner))
    return TestClient(app)


def test_private_responses_are_not_cacheable(counseling):
    c=client(counseling,counseling[3][0]);r=c.get('/v2/counseling/glossary')
    assert r.status_code==200 and r.headers.get('cache-control')=='no-store'


def test_client_cannot_choose_owner_and_cannot_access_another_session(counseling):
    a,b=counseling[3];c=client(counseling,a)
    assert c.post('/v2/counseling/sessions',headers={'X-Idempotency-Key':str(uuid4())},json={'topic':'care','owner_id':str(b)}).status_code==422
    sid=counseling[0].create(b,uuid4())['id']
    assert c.get('/v2/counseling/sessions/'+sid).status_code==404
    assert c.delete('/v2/counseling/sessions/'+sid).status_code==404


def test_http_message_is_queued_then_requires_explicit_confirmation(counseling):
    a=counseling[3][0];c=client(counseling,a)
    v=c.post('/v2/counseling/sessions',headers={'X-Idempotency-Key':str(uuid4())},json={'topic':'care'}).json()
    r=c.post('/v2/counseling/sessions/'+v['id']+'/messages',headers={'X-Idempotency-Key':str(uuid4())},
        json={'text':'그것 때문에 너무 화나.','expected_version':v['version'],'consent_event_id':counseling[4][a]})
    assert r.status_code==202 and r.json()['job']['state']=='queued'
    counseling[0].run_next()
    v=c.get('/v2/counseling/sessions/'+v['id']).json()
    assert v['phase']=='confirmation' and not v['cards'] and v['acknowledgement']
    r=c.post('/v2/counseling/sessions/'+v['id']+'/confirmation',headers={'X-Idempotency-Key':str(uuid4())},
        json={'confirmed':False,'expected_version':v['version'],'consent_event_id':counseling[4][a]})
    assert r.status_code==422 and not v['cards']


def test_full_account_erasure_also_removes_queued_counseling_records(counseling,monkeypatch,pg_pool,request):
    import os
    from api.config import Settings
    from api.main import create_app
    monkeypatch.setenv('DATABASE_URL',os.environ['YEOUN_TEST_DATABASE_URL'])
    app=create_app(Settings(os.environ['YEOUN_TEST_DATABASE_URL'],()),result_provider=None,
        transcription_provider=None,counseling_model=counseling[1],workers_enabled=False)
    with TestClient(app) as c:
        grant=c.post('/v2/sessions',json={'platform':'web'}).json()
        def cleanup_synthetic_guest():
            with pg_pool.connection() as tx:
                tx.execute('DELETE FROM p0_ai_usage_attempts WHERE owner_id=%s',(UUID(grant['user_id']),))
                tx.execute('DELETE FROM app_sessions WHERE id=%s',(UUID(grant['user_id']),))
        request.addfinalizer(cleanup_synthetic_guest)
        c.headers['Authorization']='Bearer '+grant['access_token']
        consent=c.post('/v2/me/consents',headers={'X-Idempotency-Key':str(uuid4())},
            json={'kind':'ai_processing','policy_version':'2026-11-05','granted':True}).json()
        v=c.post('/v2/counseling/sessions',headers={'X-Idempotency-Key':str(uuid4())},json={'topic':'care'}).json()
        assert c.post('/v2/counseling/sessions/'+v['id']+'/messages',headers={'X-Idempotency-Key':str(uuid4())},
            json={'text':'새로운 일을 앞두고 불안해요.','expected_version':v['version'],'consent_event_id':consent['id']}).status_code==202
        erase=c.post('/v2/me/data-requests',headers={'X-Idempotency-Key':str(uuid4())},
            json={'kind':'erase','confirmation':'내 기록 모두 삭제'}).json()
        status=c.get('/v2/data-requests/'+erase['id'],headers={'X-Data-Receipt':erase['receipt']}).json()
        assert status['state']=='completed'
        assert c.get('/v2/counseling/sessions/'+v['id']).status_code==401
