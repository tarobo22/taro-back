import base64
from datetime import datetime, timedelta, timezone
import hashlib
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from api.application.social_auth import SocialAuthService
from api.domain.social_auth import SocialAuthError, SocialAuthPolicy, VerifiedSocialIdentity, digest, pkce_challenge
from api.domain.identity import SessionRejected
from api.domain.data_privacy import DataNotFound
from api.tests.support.social_auth_fake import MemorySocialRepository, MemorySocialUnit


NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
RETURN = 'https://app.example.test/auth/complete'
VERIFIER = 'v' * 64


def setup():
    owner, other = uuid4(), uuid4()
    repository = MemorySocialRepository()
    repository.active_owners.update((owner, other))
    unit = MemorySocialUnit(repository)
    factory = Mock(return_value=unit)
    provider = Mock()
    provider.available.side_effect = lambda name: name in ('google', 'apple')
    provider.authorization_url.side_effect = lambda name, **values: 'https://provider.test/authorize?' + __import__('urllib.parse', fromlist=['urlencode']).urlencode(values)
    provider.exchange.return_value = VerifiedSocialIdentity('google', digest('fixed-provider-subject'))
    clock = [NOW]
    service = SocialAuthService(factory, provider, SocialAuthPolicy((RETURN,)), clock=lambda: clock[0])
    return service, repository, unit, provider, owner, other, clock, factory


def complete(service, provider, owner, *, name='google', subject='first', intent='sign_in'):
    provider.exchange.return_value = VerifiedSocialIdentity(name, digest(subject))
    started = service.start(owner, name, RETURN, pkce_challenge(VERIFIER), 'web', intent=intent)
    state = parse_qs(urlparse(started['authorization_url']).query)['state'][0]
    redirect = service.callback(name, state, code='synthetic-code')
    ticket = parse_qs(urlparse(redirect).fragment)['auth_ticket'][0]
    return service.exchange(owner, ticket, VERIFIER), state, ticket


def test_initial_social_account_keeps_guest_owner_and_one_time_ticket_issues_only_a_body_token():
    service, repo, unit, provider, owner, _, _, factory = setup()
    factory.assert_not_called()
    grant, state, ticket = complete(service, provider, owner)
    assert grant['user_id'] == grant['previous_user_id'] == str(owner)
    assert grant['guest_records_preserved'] is True
    assert len(grant['access_token']) == 64 and grant['access_token'] not in repr(repo.sessions)
    assert service.authenticate('Bearer ' + grant['access_token']) == owner
    assert service.account(owner) == {'user_id': str(owner), 'kind': 'member', 'providers': ['google']}
    assert state not in repo.flows and all(row.get('nonce') is None for row in repo.flows.values())
    with pytest.raises(SocialAuthError):
        service.exchange(owner, ticket, VERIFIER)
    with pytest.raises(SocialAuthError):
        service.callback('google', state, code='synthetic-code')


def test_other_device_login_switches_to_the_account_owner_without_deleting_or_merging_the_new_guest():
    service, repo, _, provider, first, second, _, _ = setup()
    one, _, _ = complete(service, provider, first, subject='same-subject')
    two, _, _ = complete(service, provider, second, subject='same-subject')
    assert two['user_id'] == str(first) and two['previous_user_id'] == str(second)
    assert second in repo.active_owners and second not in repo.accounts
    assert len(repo.sessions) == 2 and service.authenticate('Bearer ' + one['access_token']) == first


def test_same_email_is_never_used_as_a_cross_provider_identity_link():
    service, repo, _, provider, first, second, _, _ = setup()
    complete(service, provider, first, subject='provider-a-sub')
    other, _, _ = complete(service, provider, second, name='apple', subject='provider-b-sub')
    assert other['user_id'] == str(second) and len(repo.accounts) == 2


def test_explicit_link_adds_a_provider_to_the_current_owner_and_foreign_link_is_a_conflict():
    service, repo, _, provider, owner, foreign, _, _ = setup()
    complete(service, provider, owner)
    complete(service, provider, owner, name='apple', subject='apple-own', intent='link')
    assert service.account(owner)['providers'] == ['apple', 'google']
    complete(service, provider, foreign, name='apple', subject='foreign-apple')
    with pytest.raises(SocialAuthError, match='ACCOUNT_ALREADY_LINKED'):
        complete(service, provider, owner, name='apple', subject='foreign-apple', intent='link')


def test_new_provider_is_not_implicitly_linked_to_an_existing_member_on_sign_in():
    service, _, _, provider, owner, _, _, _ = setup()
    complete(service, provider, owner)
    with pytest.raises(SocialAuthError, match='ACCOUNT_LINK_REQUIRED'):
        complete(service, provider, owner, name='apple', subject='new-apple')


def test_wrong_pkce_or_foreign_owner_cannot_exchange_a_ticket():
    service, repo, _, provider, owner, foreign, _, _ = setup()
    started = service.start(owner, 'google', RETURN, pkce_challenge(VERIFIER), 'web')
    state = parse_qs(urlparse(started['authorization_url']).query)['state'][0]
    redirect = service.callback('google', state, code='code')
    ticket = parse_qs(urlparse(redirect).fragment)['auth_ticket'][0]
    with pytest.raises(SocialAuthError):
        service.exchange(foreign, ticket, VERIFIER)
    with pytest.raises(SocialAuthError):
        service.exchange(owner, ticket, 'wrong' * 15)
    assert not repo.accounts and not repo.sessions
    assert service.exchange(owner, ticket, VERIFIER)['user_id'] == str(owner)


@pytest.mark.parametrize('field,value', [('return_uri','https://evil.test/steal'),('challenge','plain'),('platform','unknown')])
def test_invalid_start_never_opens_a_unit_or_calls_provider(field,value):
    service, _, _, provider, owner, _, _, factory = setup()
    values={'return_uri':RETURN,'challenge':pkce_challenge(VERIFIER),'platform':'web'}
    values[field]=value
    with pytest.raises(SocialAuthError):
        service.start(owner, 'google', values['return_uri'], values['challenge'], values['platform'])
    factory.assert_not_called()
    provider.authorization_url.assert_not_called()


def test_unconfigured_provider_is_explicitly_unavailable_and_start_creates_nothing():
    service, repo, _, provider, owner, _, _, factory = setup()
    provider.available.return_value=False
    provider.available.side_effect=None
    assert all(not item['available'] for item in service.providers()['providers'])
    with pytest.raises(SocialAuthError, match='PROVIDER_UNAVAILABLE'):
        service.start(owner, 'google', RETURN, pkce_challenge(VERIFIER), 'web')
    assert not repo.flows
    factory.assert_not_called()


@pytest.mark.parametrize('error,expected', [('access_denied','cancelled'),('private provider text','authentication_failed')])
def test_cancel_and_provider_error_are_sanitized_and_return_only_to_the_saved_allowlist(error,expected):
    service, repo, _, provider, owner, _, _, _ = setup()
    started=service.start(owner,'apple',RETURN,pkce_challenge(VERIFIER),'ios')
    state=parse_qs(urlparse(started['authorization_url']).query)['state'][0]
    result=service.callback('apple',state,error=error)
    assert result == RETURN+'#auth_error='+expected
    assert not repo.flows and not repo.accounts
    provider.exchange.assert_not_called()


def test_callback_provider_failure_releases_locks_before_io_and_never_creates_a_session():
    service, repo, _, provider, owner, _, _, _ = setup()
    started=service.start(owner,'google',RETURN,pkce_challenge(VERIFIER),'web')
    state=parse_qs(urlparse(started['authorization_url']).query)['state'][0]
    provider.exchange.side_effect=SocialAuthError('PROVIDER_REJECTED',401)
    assert service.callback('google',state,code='code') == RETURN+'#auth_error=authentication_failed'
    assert not repo.flows and not repo.sessions


def test_flow_and_ticket_ttl_and_pending_flow_bound():
    service, repo, _, provider, owner, _, clock, _ = setup()
    for _ in range(5):service.start(owner,'google',RETURN,pkce_challenge(VERIFIER),'web')
    with pytest.raises(SocialAuthError,match='AUTH_FLOW_LIMIT'):
        service.start(owner,'google',RETURN,pkce_challenge(VERIFIER),'web')
    clock[0]+=timedelta(minutes=11)
    started=service.start(owner,'google',RETURN,pkce_challenge(VERIFIER),'web')
    assert len(repo.flows)==1
    state=parse_qs(urlparse(started['authorization_url']).query)['state'][0]
    redirect=service.callback('google',state,code='code')
    ticket=parse_qs(urlparse(redirect).fragment)['auth_ticket'][0]
    clock[0]+=timedelta(minutes=3)
    with pytest.raises(SocialAuthError):service.exchange(owner,ticket,VERIFIER)


def test_known_revoked_or_expired_session_never_falls_back_to_legacy_but_unknown_tokens_can():
    service, repo, _, provider, owner, _, clock, _ = setup()
    grant,_,_=complete(service,provider,owner)
    token='Bearer '+grant['access_token']
    assert service.authenticate('Bearer '+'f'*64) is None
    assert service.refresh(token)['session_id']==grant['session_id']
    service.logout(token)
    with pytest.raises(SessionRejected):service.authenticate(token)
    with pytest.raises(SessionRejected):service.refresh(token)
    grant,_,_=complete(service,provider,owner)
    clock[0]+=timedelta(days=181)
    with pytest.raises(SessionRejected):service.authenticate('Bearer '+grant['access_token'])


def test_session_listing_and_remote_revocation_are_owner_scoped_and_logout_all_revokes_every_device():
    service, repo, _, provider, owner, foreign, _, _ = setup()
    first,_,_=complete(service,provider,owner)
    second,_,_=complete(service,provider,owner)
    other,_,_=complete(service,provider,foreign,subject='another')
    token='Bearer '+first['access_token']
    items=service.sessions(owner,token)['items']
    assert len(items)==2 and sum(item['current'] for item in items)==1
    assert all(not set(item)&{'token_hash','access_token','provider_subject'} for item in items)
    with pytest.raises(SocialAuthError):service.revoke_session(owner,other['session_id'])
    service.revoke_session(owner,second['session_id'])
    with pytest.raises(SessionRejected):service.authenticate('Bearer '+second['access_token'])
    service.logout_all(owner)
    with pytest.raises(SessionRejected):service.authenticate(token)
    assert service.authenticate('Bearer '+other['access_token'])==foreign


def test_erasure_invalidates_pending_flow_and_account_and_sessions_without_provider_io():
    service, repo, _, provider, owner, _, _, _ = setup()
    grant,_,_=complete(service,provider,owner)
    started=service.start(owner,'google',RETURN,pkce_challenge(VERIFIER),'web')
    state=parse_qs(urlparse(started['authorization_url']).query)['state'][0]
    service.erase_owner('opaque-test-transaction',owner)
    repo.active_owners.remove(owner)
    assert not repo.accounts and not repo.sessions and not repo.flows
    with pytest.raises(SocialAuthError):service.callback('google',state,code='code')
    assert service.authenticate('Bearer '+grant['access_token']) is None
    assert service.owner_kind('opaque-test-transaction',owner)=='guest'
