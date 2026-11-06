from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from threading import Event
from urllib.parse import parse_qs, urlparse, urlencode
from uuid import UUID, uuid4
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

from api.adapters.data_privacy_postgres import PostgresDataPrivacyUnitOfWork, owner_guard
from api.adapters.social_auth_postgres import PostgresSocialUnitOfWork
from api.application.data_privacy import DataPrivacyUseCases
from api.application.identity import IdentityUseCases
from api.application.social_auth import SocialAuthService
from api.domain.data_privacy import DataPolicy
from api.domain.identity import IdentityPolicy, SessionRejected
from api.domain.social_auth import SocialAuthError, SocialAuthPolicy, VerifiedSocialIdentity, digest, pkce_challenge


pytestmark=pytest.mark.integration
ROOT=Path(__file__).parents[3]
RETURN='https://app.example.test/auth/complete'
VERIFIER='v'*64
NOW=datetime(2026,11,6,tzinfo=timezone.utc)


@pytest.fixture
def social_pg(pg_pool):
    with pg_pool.connection() as connection:
        for migration in sorted((ROOT/'api/migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    owners=[uuid4(),uuid4(),uuid4()]
    tokens=['a'+uuid4().hex+uuid4().hex[:31] for _ in owners]
    with pg_pool.connection() as connection:
        for owner,token in zip(owners,tokens):
            connection.execute('INSERT INTO app_sessions(id,token_hash,expires_at,platform) VALUES(%s,%s,%s,%s)',
                               (owner,digest(token),NOW+timedelta(days=180),'web'))
            connection.execute('INSERT INTO data_privacy_profiles(owner_id) VALUES(%s)',(owner,))
    provider=Mock()
    provider.subject_namespace=str(uuid4())
    provider.available.side_effect=lambda value:value in ('google','apple')
    provider.authorization_url.side_effect=lambda name,**values:'https://provider.test/authorize?'+urlencode(values)
    provider.exchange.return_value=VerifiedSocialIdentity('google',digest(provider.subject_namespace+':synthetic-sub'))
    clock=[NOW]
    auth=SocialAuthService(lambda:PostgresSocialUnitOfWork(pg_pool),provider,SocialAuthPolicy((RETURN,)),clock=lambda:clock[0])
    return auth,provider,owners,tokens,pg_pool,clock


def ready(auth,provider,owner,*,name='google',subject='synthetic-sub',intent='sign_in'):
    provider.exchange.return_value=VerifiedSocialIdentity(name,digest(provider.subject_namespace+':'+subject))
    result=auth.start(owner,name,RETURN,pkce_challenge(VERIFIER),'web',intent=intent)
    state=parse_qs(urlparse(result['authorization_url']).query)['state'][0]
    target=auth.callback(name,state,code='synthetic-code')
    return parse_qs(urlparse(target).fragment)['auth_ticket'][0],state


def grant(auth,provider,owner,**kwargs):
    ticket,_=ready(auth,provider,owner,**kwargs)
    return auth.exchange(owner,ticket,VERIFIER)


def privacy(pool,auth):
    policy=DataPolicy.from_mapping(json.loads((ROOT/'shared/data-policy.json').read_text(encoding='utf-8')))
    return DataPrivacyUseCases(lambda:PostgresDataPrivacyUnitOfWork(pool),policy,{},
        owner_kind=auth.owner_kind,export_callbacks=(auth.export_owner,),erase_callbacks=(auth.erase_owner,))


def test_social_signup_keeps_guest_rows_and_legacy_token_becomes_a_revocable_session(social_pg):
    auth,provider,owners,tokens,pool,_=social_pg
    entry=uuid4()
    with pool.connection() as c:
        c.execute("INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,'2026-11-06','calm','guest body')",(entry,owners[0]))
    result=grant(auth,provider,owners[0])
    assert result['user_id']==str(owners[0])
    assert auth.authenticate('Bearer '+tokens[0])==owners[0]
    assert auth.authenticate('Bearer '+result['access_token'])==owners[0]
    records=auth.sessions(owners[0],'Bearer '+result['access_token'])['items']
    assert len(records)==2 and sum(row['current'] for row in records)==1
    auth.revoke_session(owners[0],owners[0])
    with pytest.raises(SessionRejected):auth.authenticate('Bearer '+tokens[0])
    # Even the no-prefix legacy syntax is recognized as a revoked social token.
    with pytest.raises(SessionRejected):auth.authenticate(tokens[0])
    assert auth.authenticate('Bearer '+result['access_token'])==owners[0]
    with pool.connection() as c:
        assert c.execute('SELECT body FROM journal_entries WHERE id=%s AND session_id=%s',(entry,owners[0])).fetchone()['body']=='guest body'
        row=c.execute('SELECT token_hash FROM social_device_sessions WHERE id=%s',(UUID(result['session_id']),)).fetchone()
        assert row['token_hash']==digest(result['access_token'])


def test_other_device_switch_preserves_its_guest_rows_and_explicit_provider_link_is_scoped(social_pg):
    auth,provider,owners,tokens,pool,_=social_pg
    one=grant(auth,provider,owners[0])
    with pool.connection() as c:
        c.execute("INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,'2026-11-06','happy','other guest')",(uuid4(),owners[1]))
    two=grant(auth,provider,owners[1])
    assert two['user_id']==one['user_id'] and two['previous_user_id']==str(owners[1])
    assert auth.authenticate('Bearer '+tokens[1]) is None  # Legacy remains available to the root fallback.
    with pool.connection() as c:
        assert c.execute('SELECT body FROM journal_entries WHERE session_id=%s',(owners[1],)).fetchone()['body']=='other guest'
        assert c.execute('SELECT token_hash FROM app_sessions WHERE id=%s',(owners[1],)).fetchone()['token_hash']==digest(tokens[1])
    grant(auth,provider,owners[0],name='apple',subject='apple-sub',intent='link')
    assert auth.account(owners[0])['providers']==['apple','google']


def test_concurrent_ticket_exchange_can_issue_only_one_session(social_pg):
    auth,provider,owners,_,pool,_=social_pg
    ticket,_=ready(auth,provider,owners[0])
    def exchange():
        try:return auth.exchange(owners[0],ticket,VERIFIER)
        except SocialAuthError:return None
    with ThreadPoolExecutor(max_workers=2) as threads:
        results=list(threads.map(lambda _:exchange(),range(2)))
    assert sum(value is not None for value in results)==1
    with pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM social_device_sessions WHERE owner_id=%s',(owners[0],)).fetchone()['n']==2
        assert not c.execute('SELECT 1 FROM social_auth_flows WHERE owner_id=%s',(owners[0],)).fetchone()


def test_simultaneous_new_device_signins_for_one_subject_select_one_owner_without_merge(social_pg):
    auth,provider,owners,_,pool,_=social_pg
    first,_=ready(auth,provider,owners[0]);second,_=ready(auth,provider,owners[1])
    with ThreadPoolExecutor(max_workers=2) as threads:
        futures=[threads.submit(auth.exchange,owner,ticket,VERIFIER) for owner,ticket in zip(owners,(first,second))]
        results=[future.result(timeout=10) for future in futures]
    assert results[0]['user_id']==results[1]['user_id']
    with pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM social_identities WHERE subject_hash=%s',(digest(provider.subject_namespace+':synthetic-sub'),)).fetchone()['n']==1
        assert c.execute('SELECT count(*) AS n FROM app_sessions WHERE id=ANY(%s)',(owners[:2],)).fetchone()['n']==2


def test_callback_state_single_use_and_provider_runs_without_an_open_owner_lock(social_pg):
    auth,provider,owners,_,pool,_=social_pg
    started=auth.start(owners[0],'google',RETURN,pkce_challenge(VERIFIER),'web')
    state=parse_qs(urlparse(started['authorization_url']).query)['state'][0]
    nonce=parse_qs(urlparse(started['authorization_url']).query)['nonce'][0]
    with pool.connection() as c:
        assert c.execute('SELECT nonce FROM social_auth_flows WHERE state_hash=%s',(digest(state),)).fetchone()['nonce']==digest(nonce)
    def exchange(*args,**kwargs):
        assert kwargs['nonce_hash']==digest(nonce)
        with pool.connection() as c:
            assert c.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS locked',
                             (f'yeoun:owner:{owners[0]}',)).fetchone()['locked'] is True
        return VerifiedSocialIdentity('google',digest('synthetic-sub'))
    provider.exchange.side_effect=exchange
    assert 'auth_ticket=' in auth.callback('google',state,code='code')
    with pytest.raises(SocialAuthError):auth.callback('google',state,code='same-code')
    assert provider.exchange.call_count==1


def test_wrong_pkce_foreign_owner_and_foreign_session_id_cannot_change_account(social_pg):
    auth,provider,owners,_,_,_=social_pg
    ticket,_=ready(auth,provider,owners[0])
    with pytest.raises(SocialAuthError):auth.exchange(owners[1],ticket,VERIFIER)
    with pytest.raises(SocialAuthError):auth.exchange(owners[0],ticket,'wrong'*15)
    result=auth.exchange(owners[0],ticket,VERIFIER)
    with pytest.raises(SocialAuthError):auth.revoke_session(owners[1],result['session_id'])
    assert auth.authenticate('Bearer '+result['access_token'])==owners[0]


def test_all_device_logout_blocks_the_captured_legacy_session_and_keeps_account_data(social_pg):
    auth,provider,owners,tokens,pool,_=social_pg
    one=grant(auth,provider,owners[0]);two=grant(auth,provider,owners[1])
    auth.logout_all(owners[0])
    for token in (tokens[0],one['access_token'],two['access_token']):
        with pytest.raises(SessionRejected):auth.authenticate('Bearer '+token)
    assert auth.account(owners[0])['kind']=='member'
    with pool.connection() as c:
        assert c.execute('SELECT expires_at FROM app_sessions WHERE id=%s',(owners[0],)).fetchone()['expires_at']<=NOW


def test_privacy_member_profile_export_and_full_erasure_remove_social_refs_and_opaque_tokens(social_pg):
    auth,provider,owners,tokens,pool,_=social_pg
    result=grant(auth,provider,owners[0])
    data=privacy(pool,auth)
    assert data.get_me(owners[0])['kind']=='member'
    assert data.update_preferences(owners[0],{'reduced_motion':True})['kind']=='member'
    request=data.create_data_request(owners[0],'export',None,uuid4());assert data.process_request(request['id'])
    with ZipFile(io.BytesIO(data.download_export(owners[0],request['id']))) as archive:
        exported=json.loads(archive.read('data.json'))
        assert exported['profile']['kind']=='member' and exported['social_accounts'][0]['provider']=='google'
        assert not any(word in json.dumps(exported['social_accounts']) for word in ('subject_hash','token_hash','nonce'))
    pending=auth.start(owners[0],'google',RETURN,pkce_challenge(VERIFIER),'web')
    state=parse_qs(urlparse(pending['authorization_url']).query)['state'][0]
    erase=data.create_data_request(owners[0],'erase',data.policy.erase_confirmation,uuid4());assert data.process_request(erase['id'])
    with pool.connection() as c:
        for table in ('social_accounts','social_identities','social_device_sessions','social_auth_flows'):
            assert not c.execute(f'SELECT 1 FROM {table} WHERE owner_id=%s',(owners[0],)).fetchone()
        assert not owner_guard(c,owners[0])
    assert auth.authenticate('Bearer '+result['access_token']) is None
    with pytest.raises(SocialAuthError):auth.callback('google',state,code='code')


def test_erasure_during_provider_io_prevents_late_callback_from_recreating_any_identity(social_pg):
    auth,provider,owners,_,pool,_=social_pg
    data=privacy(pool,auth)
    pending=auth.start(owners[0],'google',RETURN,pkce_challenge(VERIFIER),'web')
    state=parse_qs(urlparse(pending['authorization_url']).query)['state'][0]
    entered,release=Event(),Event()
    def exchange(*args,**kwargs):
        entered.set();assert release.wait(5)
        return VerifiedSocialIdentity('google',digest('late-sub'))
    provider.exchange.side_effect=exchange
    with ThreadPoolExecutor(max_workers=1) as threads:
        future=threads.submit(auth.callback,'google',state,code='code')
        assert entered.wait(2)
        request=data.create_data_request(owners[0],'erase',data.policy.erase_confirmation,uuid4())
        assert data.process_request(request['id'])
        release.set()
        assert future.result(timeout=10)==RETURN+'#auth_error=authentication_failed'
    with pool.connection() as c:
        for table in ('social_accounts','social_identities','social_device_sessions','social_auth_flows'):
            assert not c.execute(f'SELECT 1 FROM {table} WHERE owner_id=%s',(owners[0],)).fetchone()
