from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.adapters.http.social_auth import social_auth_router
from api.domain.social_auth import pkce_challenge
from api.tests.unit.test_social_auth import RETURN, VERIFIER, setup


def context():
    service,repo,_,provider,owner,foreign,clock,_=setup()
    current=[owner]
    app=FastAPI()
    app.include_router(social_auth_router(service,lambda:current[0]))
    return TestClient(app),service,repo,provider,owner,foreign,current,clock


@pytest.mark.parametrize('provider,method', [('google','GET'),('apple','POST')])
def test_http_oauth_callback_returns_fragment_ticket_and_json_exchange_only_contains_the_bearer_in_its_body(provider,method):
    from api.domain.social_auth import VerifiedSocialIdentity,digest
    client,service,repo,adapter,owner,_,_,_=context()
    adapter.exchange.return_value=VerifiedSocialIdentity(provider,digest('subject'))
    with client:
        started=client.post('/v2/auth/social/'+provider+'/start',json={'return_uri':RETURN,'code_challenge':pkce_challenge(VERIFIER),'platform':'web'})
        assert started.status_code==201 and started.headers['cache-control']=='no-store'
        state=parse_qs(urlparse(started.json()['authorization_url']).query)['state'][0]
        path='/v2/auth/social/'+provider+'/callback'
        response=(client.get(path,params={'state':state,'code':'synthetic-code'},follow_redirects=False) if method=='GET'
                  else client.post(path,data={'state':state,'code':'synthetic-code','user':'untrusted private profile'},follow_redirects=False))
        assert response.status_code==303 and response.headers['referrer-policy']=='no-referrer'
        target=urlparse(response.headers['location']);assert not target.query
        fragment=parse_qs(target.fragment);assert fragment['auth_provider']==[provider]
        exchanged=client.post('/v2/auth/social/exchange',json={'ticket':fragment['auth_ticket'][0],'code_verifier':VERIFIER})
        assert exchanged.status_code==201 and exchanged.headers['cache-control']=='no-store'
        grant=exchanged.json();assert grant['user_id']==str(owner) and grant['guest_records_preserved'] is True
        assert grant['access_token'] not in response.headers['location'] and len(grant['access_token'])==64
        assert client.post('/v2/auth/logout',headers={'Authorization':'Bearer '+grant['access_token']}).status_code==204


def test_http_unavailable_and_bad_input_do_not_mutate_any_owner():
    client,_,repo,provider,_,_,_,_=context()
    provider.available.side_effect=None;provider.available.return_value=False
    with client:
        assert all(not value['available'] for value in client.get('/v2/auth/providers').json()['providers'])
        assert client.post('/v2/auth/social/google/start',json={'return_uri':RETURN,'code_challenge':pkce_challenge(VERIFIER),'platform':'web'}).status_code==503
        assert client.post('/v2/auth/social/google/start',json={'return_uri':RETURN,'code_challenge':'plain','platform':'web'}).status_code==422
        assert client.post('/v2/auth/social/apple/callback',json={'state':'s'*43,'code':'code'}).status_code==415
        assert client.get('/v2/auth/social/google/callback?state=a&state=b').status_code==400
        assert client.get('/v2/auth/social/google/callback?state='+('z'*43)).status_code==400
    assert not repo.flows and not repo.sessions and not repo.accounts


def test_http_provider_cancellation_redirects_only_a_fixed_error_code_to_saved_return_uri():
    client,_,_,_,_,_,_,_=context()
    with client:
        started=client.post('/v2/auth/social/apple/start',json={'return_uri':RETURN,'code_challenge':pkce_challenge(VERIFIER),'platform':'ios'}).json()
        state=parse_qs(urlparse(started['authorization_url']).query)['state'][0]
        response=client.post('/v2/auth/social/apple/callback',data={'state':state,'error':'access_denied','error_description':'private words'},follow_redirects=False)
        assert response.status_code==303 and response.headers['location']==RETURN+'#auth_error=cancelled'
        assert 'private' not in response.text+response.headers['location']
