from datetime import datetime, timedelta, timezone
import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric import ec, rsa
import httpx
import jwt
import pytest

from api.adapters.oidc_social import OIDCProviderConfig, OIDCSocialProvider, apple_client_secret
from api.domain.social_auth import SocialAuthError, digest, pkce_challenge


@pytest.fixture(scope='module')
def keys():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048), rsa.generate_private_key(public_exponent=65537, key_size=2048)


def oidc(keys, provider='google', *, claims=None, algorithm='RS256', signed_by_other=False, missing_token=False):
    now=datetime.now(timezone.utc)
    values={'iss':'https://accounts.google.com' if provider=='google' else 'https://appleid.apple.com',
            'aud':'synthetic-client','sub':'synthetic-subject','nonce':'expected-nonce',
            'iat':int(now.timestamp()),'exp':int((now+timedelta(minutes=5)).timestamp()),
            'email':'not-an-account-key@example.test'}
    values.update(claims or {})
    key=keys[1] if signed_by_other else keys[0]
    if algorithm=='HS256':key='synthetic-hmac-key-which-is-long-enough-1234567890'
    encoded=jwt.encode(values,key,algorithm=algorithm,headers={'kid':'fixture-key'})
    jwk=json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(keys[0].public_key()))
    jwk.update(kid='fixture-key',use='sig',alg='RS256')
    calls=[]
    def transport(request):
        calls.append(request)
        if request.url.path.endswith('/token'):
            return httpx.Response(200,json={} if missing_token else {'id_token':encoded,'access_token':'discard-this-provider-token','refresh_token':'discard-this-refresh-token'})
        return httpx.Response(200,json={'keys':[jwk]})
    client=httpx.Client(transport=httpx.MockTransport(transport))
    secret=Mock(return_value='generated-test-client-secret')
    config=OIDCProviderConfig('synthetic-client','https://api.example.test/v2/auth/social/'+provider+'/callback',secret)
    service=OIDCSocialProvider(client,{provider:config})
    return service,calls,secret,encoded,values


@pytest.mark.parametrize('provider', ['google','apple'])
def test_signed_subject_validation_uses_fixed_provider_endpoints_and_keeps_no_email_or_provider_token(keys,provider):
    service,calls,secret,_,claims=oidc(keys,provider)
    secret.assert_not_called()
    result=service.exchange(provider,'synthetic-code',nonce_hash=digest('expected-nonce'),verifier='v'*64)
    assert result.provider==provider
    assert result.subject_hash==digest(claims['iss']+'\0'+claims['sub'])
    assert 'email' not in repr(result) and 'discard-this' not in repr(vars(service))
    data=parse_qs(calls[0].content.decode())
    assert data['code']==['synthetic-code'] and data['grant_type']==['authorization_code']
    if provider=='google':assert data['code_verifier']==['v'*64]
    else:assert 'code_verifier' not in data
    assert calls[1].url.host in ('www.googleapis.com','appleid.apple.com')


@pytest.mark.parametrize('provider', ['google','apple'])
def test_authorization_url_protects_state_and_nonce_without_secret_or_pkce_verifier(keys,provider):
    service,calls,secret,_,_=oidc(keys,provider)
    url=service.authorization_url(provider,state='opaque-state',nonce='opaque-nonce',verifier='v'*64)
    query=parse_qs(urlsplit(url).query)
    assert query['state']==['opaque-state'] and query['nonce']==['opaque-nonce']
    assert query['response_type']==['code'] and 'client_secret' not in query and 'code_verifier' not in query
    if provider=='google':assert query['code_challenge']==[pkce_challenge('v'*64)] and query['code_challenge_method']==['S256']
    else:assert query['response_mode']==['form_post']
    assert calls==[]
    secret.assert_not_called()


@pytest.mark.parametrize('change', [{'iss':'https://accounts.google.com.evil.test'}, {'aud':'foreign-client'},
    {'nonce':'replayed-nonce'}, {'exp':1}, {'iat':9999999999}, {'sub':''}, {'sub':'contains spaces'},
    {'aud':['synthetic-client','other-client']}, {'azp':'other-client'}])
def test_invalid_signature_claims_or_nonce_never_authenticate(keys,change):
    service,_,_,token,_=oidc(keys,claims=change)
    with pytest.raises(SocialAuthError,match='PROVIDER_IDENTITY_INVALID'):
        service.verify('google',token,digest('expected-nonce'))


def test_other_signing_key_and_symmetric_algorithm_are_rejected(keys):
    for options in ({'signed_by_other':True},{'algorithm':'HS256'}):
        service,_,_,token,_=oidc(keys,**options)
        with pytest.raises(SocialAuthError,match='PROVIDER_IDENTITY_INVALID'):
            service.verify('google',token,digest('expected-nonce'))


def test_missing_id_token_or_unconfigured_provider_has_no_fake_success(keys):
    service,_,_,_,_=oidc(keys,missing_token=True)
    with pytest.raises(SocialAuthError):service.exchange('google','code',nonce_hash=digest('expected-nonce'),verifier='v'*64)
    empty=OIDCSocialProvider(httpx.Client(transport=httpx.MockTransport(lambda request:pytest.fail('no request expected'))))
    assert not empty.available('google') and not empty.available('apple')
    with pytest.raises(SocialAuthError,match='PROVIDER_UNAVAILABLE'):
        empty.authorization_url('google',state='state',nonce='nonce',verifier='v'*64)


def test_apple_assertion_uses_generated_ec_key_only_and_has_a_five_minute_expiry():
    key=ec.generate_private_key(ec.SECP256R1())
    now=datetime.now(timezone.utc)
    encoded=apple_client_secret('synthetic-team','fixture-kid','synthetic-client',lambda:key,clock=lambda:now)
    header=jwt.get_unverified_header(encoded)
    claims=jwt.decode(encoded,key.public_key(),algorithms=['ES256'],audience='https://appleid.apple.com',issuer='synthetic-team')
    assert header['kid']=='fixture-kid' and claims['sub']=='synthetic-client'
    assert claims['exp']-claims['iat']==300
