from fastapi import FastAPI
from fastapi.testclient import TestClient
from uuid import uuid4

from api.adapters.http.admin import admin_router
from api.tests.unit.test_admin import NOW, setup


def context(role='publisher'):
    service,repo,owner,normal,catalog,ids,clock,_=setup(role)
    app=FastAPI();app.include_router(admin_router(service))
    return TestClient(app),service,repo,owner,normal,ids,clock


def header(token):return {'Authorization':'Bearer '+token}


def token(client,normal):
    response=client.post('/v2/admin/sessions',headers=header(normal))
    assert response.status_code==201 and response.headers['cache-control']=='no-store'
    assert set(response.json())=={'access_token','session_id','owner_id','role','expires_at'}
    return response.json()['access_token']


def test_no_role_has_no_eligibility_and_user_bearer_never_opens_an_admin_operation():
    client,_,repo,_,normal,_,_=context(None)
    with client:
        response=client.get('/v2/admin/eligibility',headers=header(normal))
        assert response.json()=={'eligible':False,'role':None,'session_seconds':900}
        assert client.post('/v2/admin/sessions',headers=header(normal)).status_code==403
        for path in ('/me','/catalog','/feedback','/audit','/ota-policy'):
            denied=client.get('/v2/admin'+path,headers=header(normal))
            assert denied.status_code==401 and denied.headers['cache-control']=='no-store'
    assert not repo.sessions


def test_viewer_reads_79_public_asset_dtos_and_cannot_mutate():
    client,_,repo,_,normal,ids,_=context('viewer')
    with client:
        bearer=token(client,normal);headers=header(bearer)
        assert client.get('/v2/admin/eligibility',headers=header(normal)).json()['role']=='viewer'
        assert client.get('/v2/admin/me',headers=headers).json()['audit_retention_days'] is None
        catalog=client.get('/v2/admin/catalog',headers=headers)
        assert catalog.status_code==200 and len(catalog.json()['items'][0]['assets'])==79
        assert catalog.json()['items'][0]['review_note']==''
        assert set(catalog.json()['items'][0]['assets'][0])=={'id','url','sha256','width','height','source_page','license_name','license_url'}
        assert client.post('/v2/admin/catalog/'+ids[0]+'/review',headers=headers,
            json={'expected_review_version':0,'rights_status':'approved','notes':'fixture'}).status_code==403
        assert client.patch('/v2/admin/feedback/'+str(uuid4())+'/triage',headers=headers,
            json={'expected_version':0,'status':'resolved'}).status_code==403
    assert not repo.versions


def test_publisher_review_publish_rollback_requires_explicit_snapshot_and_cas():
    client,_,repo,_,normal,ids,_=context()
    with client:
        headers=header(token(client,normal));path='/v2/admin/catalog/'+ids[0]
        assert client.post(path+'/publish',headers=headers,json={'expected_review_version':0,'expected_publication_version':0}).status_code==409
        approved=client.post(path+'/review',headers=headers,json={'expected_review_version':0,'rights_status':'approved','notes':'fixture rights checked'})
        assert approved.status_code==200 and not approved.json()['items'][0]['published']
        published=client.post(path+'/publish',headers=headers,json={'expected_review_version':1,'expected_publication_version':0})
        assert published.status_code==200
        publication=published.json()['items'][0]['current_publication_id']
        assert client.post(path+'/publish',headers=headers,json={'expected_review_version':1,'expected_publication_version':0}).status_code==409
        assert client.post(path+'/rollback',headers=headers,json={'expected_publication_version':1}).status_code==422
        assert client.post(path+'/rollback',headers=headers,json={'publication_id':str(uuid4()),'expected_publication_version':1}).status_code==404
        rolled=client.post(path+'/rollback',headers=headers,json={'publication_id':publication,'expected_publication_version':1})
        assert rolled.status_code==200 and rolled.json()['items'][0]['publication_version']==2
        assert 'catalog.rollback' in [row['action'] for row in client.get('/v2/admin/audit',headers=headers).json()['items']]
    assert len(repo.publications)==2


def test_http_mutation_input_is_strict_and_no_role_grant_route_exists():
    client,_,_,_,normal,ids,_=context()
    with client:
        headers=header(token(client,normal));path='/v2/admin/catalog/'+ids[0]
        for expected in (True,'0',-1):
            assert client.post(path+'/review',headers=headers,json={'expected_review_version':expected,'rights_status':'approved','notes':'fixture'}).status_code==422
        assert client.post(path+'/review',headers=headers,json={'expected_review_version':0,'rights_status':'approved','notes':'fixture','role':'publisher'}).status_code==422
        assert client.put('/v2/admin/ota-policy',headers=headers,json={'expected_version':0,'channel':'preview','min_native_version':'1.3.0','updates_enabled':'false'}).status_code==422
        assert client.get('/v2/admin/feedback?limit=51',headers=headers).status_code==422
        assert client.get('/v2/admin/audit?cursor=not-a-cursor',headers=headers).status_code==422
        assert client.post('/v2/admin/roles',headers=headers,json={'owner_id':str(uuid4()),'role':'publisher'}).status_code==404


def test_ota_cas_and_logout_statuses_keep_the_public_policy_seam_in_sync():
    client,service,_,_,normal,_,_=context()
    with client:
        headers=header(token(client,normal))
        paused=client.put('/v2/admin/ota-policy',headers=headers,json={'expected_version':0,'channel':'preview','min_native_version':'1.3.0','updates_enabled':False})
        assert paused.status_code==200 and paused.json()['version']==1
        assert service.ota_policy_for_bootstrap()=={'channel':'preview','min_native_version':'1.3.0','updates_enabled':False}
        assert client.put('/v2/admin/ota-policy',headers=headers,json={'expected_version':0,'channel':'preview','min_native_version':'1.3.0','updates_enabled':True}).status_code==409
        assert client.delete('/v2/admin/sessions/current',headers=headers).status_code==204
        assert client.get('/v2/admin/me',headers=headers).status_code==401


def test_feedback_triage_returns_public_feedback_fields_and_omits_user_content_from_audit():
    client,_,repo,_,normal,_,_=context();feedback_id=uuid4()
    repo.feedback_rows[str(feedback_id)]={'id':feedback_id,'reading_id':None,'turn_id':None,'rating':'unhelpful',
        'reason':'feedback','comment':'private fixture comment','created_at':NOW,'triage_status':'new','triage_version':0}
    with client:
        headers=header(token(client,normal))
        assert client.get('/v2/admin/feedback?status=new',headers=headers).json()['items'][0]['comment']=='private fixture comment'
        updated=client.patch('/v2/admin/feedback/'+str(feedback_id)+'/triage',headers=headers,json={'expected_version':0,'status':'reviewing'})
        assert updated.status_code==200 and updated.json()['triage_version']==1
        assert client.patch('/v2/admin/feedback/'+str(feedback_id)+'/triage',headers=headers,json={'expected_version':0,'status':'resolved'}).status_code==409
        assert 'private fixture comment' not in client.get('/v2/admin/audit',headers=headers).text
