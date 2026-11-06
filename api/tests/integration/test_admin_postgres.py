from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import pytest

from api.adapters.admin_postgres import PostgresAdminRepository, PostgresAdminUnitOfWork
from api.adapters.data_privacy_postgres import (PostgresDataPrivacyRepository, PostgresDataPrivacyUnitOfWork,
                                               initialize_profile, lock_owner, owner_guard)
from api.adapters.identity_postgres import PostgresIdentityUnitOfWork
from api.adapters.social_auth_postgres import PostgresSocialUnitOfWork
from api.application.admin import AdminRoleManager, AdminService
from api.application.data_privacy import DataPrivacyUseCases
from api.application.identity import IdentityUseCases
from api.application.social_auth import SocialAuthService
from api.domain.admin import AdminError, AdminPolicy
from api.domain.data_privacy import DataPolicy
from api.domain.identity import IdentityPolicy, SessionRejected
from api.domain.social_auth import SocialAuthPolicy, digest
from api.tests.support.admin_fake import catalog_fixture


pytestmark=pytest.mark.integration
ROOT=Path(__file__).parents[3]
NOW=datetime(2026,11,6,tzinfo=timezone.utc)
OTA={'channel':'preview','min_native_version':'1.3.0','updates_enabled':True}


@pytest.fixture
def admin_pg():
    dsn=os.environ.get('YEOUN_TEST_DATABASE_URL')
    if not dsn or not conninfo_to_dict(dsn).get('dbname','').endswith('_test'):
        raise RuntimeError('An isolated _test database is required')
    namespace='admin_'+uuid4().hex
    with psycopg.connect(dsn) as c:
        c.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(namespace)))
    options={'row_factory':dict_row,'options':'-c search_path='+namespace+',pg_catalog'}
    with ConnectionPool(dsn,min_size=1,max_size=5,kwargs=options) as pool, \
            ConnectionPool(dsn,min_size=0,max_size=2,kwargs=options) as query_pool:
        pool.wait(timeout=15)
        with pool.connection() as c:
            for path in sorted((ROOT/'api/migrations').glob('*.sql')):
                c.execute(path.read_text(encoding='utf-8'))
        owner,guest,source=uuid4(),uuid4(),uuid4();normal=uuid4().hex+uuid4().hex
        with pool.connection() as c:
            for value in (owner,guest):
                c.execute('INSERT INTO app_sessions(id,token_hash,expires_at,platform) VALUES(%s,%s,%s,%s)',
                          (value,digest(normal) if value==owner else digest(str(value)),NOW+timedelta(days=180),'web'))
                c.execute('INSERT INTO data_privacy_profiles(owner_id) VALUES(%s)',(value,))
            c.execute('INSERT INTO social_accounts(owner_id) VALUES(%s)',(owner,))
            c.execute('''INSERT INTO social_device_sessions(id,owner_id,token_hash,platform,expires_at)
                VALUES(%s,%s,%s,'web',%s)''',(source,owner,digest(normal),NOW+timedelta(days=2)))
        clock=[NOW];factory=lambda:PostgresAdminUnitOfWork(pool)
        auth=SocialAuthService(lambda:PostgresSocialUnitOfWork(pool),Mock(),SocialAuthPolicy(),clock=lambda:clock[0])
        catalog,ids=catalog_fixture()
        service=AdminService(factory,auth,catalog,query_uow_factory=lambda:PostgresAdminUnitOfWork(query_pool),
            ota_defaults=OTA,clock=lambda:clock[0])
        manager=AdminRoleManager(factory,clock=lambda:clock[0])
        yield SimpleNamespace(service=service,pool=pool,query_pool=query_pool,owner=owner,guest=guest,source=source,
            normal=normal,clock=clock,catalog=catalog,ids=ids,manager=manager,auth=auth,factory=factory)
    # Only this fixture's random schema is removed; no existing owners or schemas.
    with psycopg.connect(dsn) as c:
        c.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(namespace)))


def issued(ctx,role='publisher'):
    ctx.manager.grant(ctx.owner,role)
    return 'Bearer '+ctx.service.issue('Bearer '+ctx.normal)['access_token']


def approve(ctx,bearer,index=0):
    return ctx.service.review(bearer,ctx.ids[index],0,'approved','fixture source and license reviewed')


def add_feedback(ctx,owner=None,*,created_at=NOW):
    identity=uuid4()
    with ctx.pool.connection() as c:
        c.execute('''INSERT INTO data_privacy_feedback(id,owner_id,rating,reason,comment,idempotency_hash,fingerprint,created_at)
            VALUES(%s,%s,'unhelpful','fixture','private fixture comment',%s,%s,%s)''',
            (identity,owner or ctx.guest,str(uuid4()),'f'*64,created_at))
    return identity


def test_role_grants_are_explicit_member_only_and_admin_bearers_are_short_hashed_separate_credentials(admin_pg):
    ctx=admin_pg
    assert ctx.service.eligibility('Bearer '+ctx.normal)=={'eligible':False,'role':None,'session_seconds':900}
    with pytest.raises(AdminError,match='ADMIN_FORBIDDEN'):ctx.service.issue('Bearer '+ctx.normal)
    with pytest.raises(AdminError,match='MEMBER_REQUIRED'):ctx.manager.grant(ctx.guest,'publisher')
    bearer=issued(ctx,'viewer');me=ctx.service.me(bearer)
    assert me['role']=='viewer' and me['expires_at']==(NOW+timedelta(seconds=900)).isoformat()
    with ctx.pool.connection() as c:
        row=c.execute('SELECT * FROM admin_sessions').fetchone()
        assert row['token_hash']==digest(bearer.removeprefix('Bearer ')) and bearer not in repr(row)
    # Preserve the existing social -> legacy authentication composition. Both
    # stages must fail to resolve a user for a separately prefixed admin token.
    assert ctx.auth.authenticate(bearer) is None
    identity_values=json.loads((ROOT/'shared/identity-policy.json').read_text(encoding='utf-8'))
    identity_policy=IdentityPolicy(identity_values['session_days'],tuple(identity_values['locales']))
    legacy=IdentityUseCases(lambda:PostgresIdentityUnitOfWork(ctx.pool,initialize_profile,owner_guard),
        identity_policy,lambda:NOW,uuid4)
    with pytest.raises(SessionRejected):legacy.authenticate(bearer)
    with pytest.raises(AdminError,match='ADMIN_SESSION_INVALID'):ctx.service.catalog('Bearer '+ctx.normal)
    with pytest.raises(AdminError,match='ADMIN_FORBIDDEN'):approve(ctx,bearer)


@pytest.mark.parametrize('invalidation',['source_logout','source_expiry','role_change','owner_erasure','admin_ttl'])
def test_every_admin_request_rechecks_source_role_owner_and_admin_expiry(admin_pg,invalidation):
    ctx=admin_pg;bearer=issued(ctx)
    if invalidation=='source_logout':ctx.auth.logout('Bearer '+ctx.normal)
    elif invalidation=='source_expiry':
        with ctx.pool.connection() as c:c.execute('UPDATE social_device_sessions SET expires_at=%s WHERE id=%s',(NOW,ctx.source))
    elif invalidation=='role_change':ctx.manager.grant(ctx.owner,'viewer')
    elif invalidation=='owner_erasure':
        with ctx.pool.connection() as c:c.execute("UPDATE data_privacy_profiles SET state='erased' WHERE owner_id=%s",(ctx.owner,))
    else:ctx.clock[0]+=timedelta(minutes=16)
    with pytest.raises(AdminError,match='ADMIN_SESSION_INVALID'):ctx.service.me(bearer)


def test_publish_and_explicit_prior_snapshot_rollback_preserve_immutable_assets_and_public_queries(admin_pg):
    ctx=admin_pg;bearer=issued(ctx)
    assert not any(row['published'] for row in ctx.service.catalog(bearer)['items'])
    with pytest.raises(AdminError,match='CATALOG_NOT_APPROVED'):ctx.service.publish(bearer,ctx.ids[0],0,0)
    approve(ctx,bearer);first=ctx.service.publish(bearer,ctx.ids[0],1,0)
    first_id=next(row for row in first['items'] if row['version_id']==ctx.ids[0])['current_publication_id']
    approve(ctx,bearer,1);ctx.service.publish(bearer,ctx.ids[1],1,1)
    assert next(row for row in ctx.service.catalog_source()['decks'] if row['id']==ctx.ids[0])['status']=='retired'
    restored=ctx.service.rollback(bearer,ctx.ids[0],first_id,2)
    assert next(row for row in restored['items'] if row['version_id']==ctx.ids[0])['published']
    source=ctx.service.catalog_source()
    assert source['cards']==ctx.catalog['cards_by_deck'][ctx.ids[0]]
    assert source['cards_by_deck'][ctx.ids[1]]==ctx.catalog['cards_by_deck'][ctx.ids[1]]
    with ctx.pool.connection() as c:
        publications=c.execute('SELECT snapshot,action FROM admin_catalog_publications').fetchall()
        assert len(publications)==3 and sum(row['action']=='rollback' for row in publications)==1
        first_snapshot=c.execute('SELECT snapshot FROM admin_catalog_publications WHERE id=%s',(first_id,)).fetchone()['snapshot']
        assert first_snapshot['cards']==ctx.catalog['cards_by_deck'][ctx.ids[0]]
        audit=c.execute("SELECT details FROM admin_audit WHERE action='catalog.rollback'").fetchone()
        assert audit['details']['publication_version']==3


def test_stale_concurrent_publish_has_exactly_one_winner_and_one_audited_snapshot(admin_pg):
    ctx=admin_pg;bearer=issued(ctx);approve(ctx,bearer)
    def run():
        try:ctx.service.publish(bearer,ctx.ids[0],1,0);return 'published'
        except AdminError as error:return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results=list(executor.map(lambda _:run(),range(2)))
    assert sorted(results)==['STALE_PUBLICATION','published']
    with ctx.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM admin_catalog_publications').fetchone()['n']==1
        assert c.execute("SELECT count(*) AS n FROM admin_audit WHERE action='catalog.publish'").fetchone()['n']==1


def test_public_catalog_snapshot_cannot_mix_heads_from_concurrent_publication_changes(admin_pg,monkeypatch):
    ctx=admin_pg;bearer=issued(ctx);approve(ctx,bearer);ctx.service.publish(bearer,ctx.ids[0],1,0);approve(ctx,bearer,1)
    reading_head,release_read,write_attempt=Event(),Event(),Event()
    original_head,original_lock=PostgresAdminRepository.head,PostgresAdminRepository.lock_governance
    paused=[False]
    def head(repo,code):
        row=original_head(repo,code)
        if not paused[0]:
            paused[0]=True;reading_head.set()
            assert release_read.wait(5)
        return row
    def lock(repo,*args,**kwargs):
        if not kwargs.get('shared',False):write_attempt.set()
        return original_lock(repo,*args,**kwargs)
    monkeypatch.setattr(PostgresAdminRepository,'head',head)
    monkeypatch.setattr(PostgresAdminRepository,'lock_governance',lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reading=executor.submit(ctx.service.catalog_source)
        try:
            assert reading_head.wait(5)
            writing=executor.submit(ctx.service.publish,bearer,ctx.ids[1],1,1)
            assert write_attempt.wait(5)
            with pytest.raises(FutureTimeout):writing.result(timeout=0.2)
        finally:release_read.set()
        snapshot=reading.result(timeout=5);writing.result(timeout=5)
    assert [row['id'] for row in snapshot['decks'] if row['status']=='published']==[ctx.ids[0]]
    assert [row['id'] for row in ctx.service.catalog_source()['decks'] if row['status']=='published']==[ctx.ids[1]]


def test_rights_rejection_withdraws_without_auto_republication_and_source_mutation_is_rejected(admin_pg):
    ctx=admin_pg;bearer=issued(ctx);approve(ctx,bearer);ctx.service.publish(bearer,ctx.ids[0],1,0)
    ctx.service.review(bearer,ctx.ids[0],1,'rejected','fixture permission revoked')
    assert not any(row['status']=='published' for row in ctx.service.catalog_source()['decks'])
    ctx.service.review(bearer,ctx.ids[0],2,'approved','fixture evidence renewed')
    assert not any(row['published'] for row in ctx.service.catalog(bearer)['items'])
    ctx.service.source_catalog['cards_by_deck'][ctx.ids[0]][0]['image_asset']['id']=str(uuid4())
    with pytest.raises(AdminError,match='IMMUTABLE_CATALOG_VERSION'):ctx.service.catalog_source()


def test_feedback_triage_cas_keyset_paging_and_audit_never_copy_feedback_content(admin_pg):
    ctx=admin_pg;bearer=issued(ctx)
    ids=[add_feedback(ctx,created_at=NOW+timedelta(seconds=n)) for n in range(3)]
    first=ctx.service.feedback(bearer,limit=1,status='new')
    second=ctx.service.feedback(bearer,limit=1,status='new',cursor=first['next_cursor'])
    assert [first['items'][0]['id'],second['items'][0]['id']]==ids[::-1][:2]
    assert ctx.service.triage(bearer,ids[0],0,'reviewing')['triage_version']==1
    with pytest.raises(AdminError,match='STALE_FEEDBACK'):ctx.service.triage(bearer,ids[0],0,'resolved')
    assert len(ctx.service.feedback(bearer,status='new')['items'])==2
    with pytest.raises(AdminError,match='INVALID_ADMIN_CURSOR'):
        ctx.service.feedback(bearer,limit=1,status='resolved',cursor=first['next_cursor'])
    audit=ctx.service.audits(bearer,limit=1)
    assert audit['next_cursor'] and 'private fixture comment' not in repr(ctx.service.audits(bearer))
    seen={str(audit['items'][0]['id'])}
    while audit['next_cursor']:
        audit=ctx.service.audits(bearer,limit=1,cursor=audit['next_cursor'])
        assert str(audit['items'][0]['id']) not in seen
        seen.add(str(audit['items'][0]['id']))


def test_feedback_erasure_cascades_triage_and_cannot_be_recreated_by_stale_admin_mutation(admin_pg):
    ctx=admin_pg;bearer=issued(ctx);feedback=add_feedback(ctx)
    ctx.service.triage(bearer,feedback,0,'resolved')
    with ctx.pool.connection() as c:c.execute('DELETE FROM data_privacy_feedback WHERE id=%s',(feedback,))
    with pytest.raises(AdminError,match='FEEDBACK_NOT_FOUND'):ctx.service.triage(bearer,feedback,1,'reviewing')
    with ctx.pool.connection() as c:
        assert c.execute('SELECT 1 FROM admin_feedback_triage WHERE feedback_id=%s',(feedback,)).fetchone() is None


def test_concurrent_feedback_owner_erase_wins_without_recreating_triage_or_copying_private_feedback(admin_pg,monkeypatch):
    ctx=admin_pg;bearer=issued(ctx);feedback=add_feedback(ctx);observed=Event()
    original=PostgresAdminRepository.triage
    def triage(repo,*args):
        observed.set();return original(repo,*args)
    monkeypatch.setattr(PostgresAdminRepository,'triage',triage)
    with ctx.pool.connection() as c, ThreadPoolExecutor(max_workers=1) as executor:
        lock_owner(c,ctx.guest);PostgresDataPrivacyRepository(c).erase_legacy(ctx.guest)
        future=executor.submit(ctx.service.triage,bearer,feedback,0,'resolved')
        try:
            assert observed.wait(5)
            c.commit()
            with pytest.raises(AdminError,match='STALE_FEEDBACK'):future.result(timeout=5)
        finally:c.rollback()
    with ctx.pool.connection() as c:
        assert c.execute('SELECT 1 FROM admin_feedback_triage WHERE feedback_id=%s',(feedback,)).fetchone() is None
        assert c.execute("SELECT 1 FROM admin_audit WHERE action='feedback.triage'").fetchone() is None


def test_full_privacy_erasure_removes_admin_role_sessions_and_anonymizes_audit_atomically(admin_pg):
    ctx=admin_pg;bearer=issued(ctx);feedback=add_feedback(ctx,ctx.owner);ctx.service.triage(bearer,feedback,0,'resolved')
    policy=DataPolicy.from_mapping(json.loads((ROOT/'shared/data-policy.json').read_text(encoding='utf-8')))
    privacy=DataPrivacyUseCases(lambda:PostgresDataPrivacyUnitOfWork(ctx.pool),policy,ctx.catalog,
        erase_callbacks=(ctx.service.erase_owner,ctx.auth.erase_owner),owner_kind=ctx.auth.owner_kind,clock=lambda:NOW)
    accepted=privacy.create_data_request(ctx.owner,'erase',policy.erase_confirmation,uuid4())
    assert privacy.process_request(UUID(accepted['id'])) is True
    with pytest.raises(AdminError,match='ADMIN_SESSION_INVALID'):ctx.service.me(bearer)
    with ctx.pool.connection() as c:
        assert c.execute('SELECT 1 FROM admin_roles WHERE owner_id=%s',(ctx.owner,)).fetchone() is None
        assert c.execute('SELECT 1 FROM admin_sessions WHERE owner_id=%s',(ctx.owner,)).fetchone() is None
        assert c.execute('SELECT 1 FROM admin_audit WHERE actor_owner_id=%s',(ctx.owner,)).fetchone() is None
        assert c.execute('SELECT 1 FROM admin_audit WHERE target_id=%s',(str(ctx.owner),)).fetchone() is None
        assert c.execute('SELECT 1 FROM admin_feedback_triage WHERE feedback_id=%s',(feedback,)).fetchone() is None
        assert c.execute('SELECT count(*) AS n FROM admin_audit').fetchone()['n']>=3


def test_ota_cas_pause_resume_and_dedicated_read_pool_operate_with_the_main_pool_exhausted(admin_pg):
    ctx=admin_pg;bearer=issued(ctx)
    assert ctx.service.set_ota(bearer,0,{**OTA,'updates_enabled':False})['version']==1
    with pytest.raises(AdminError,match='STALE_OTA_POLICY'):ctx.service.set_ota(bearer,0,OTA)
    held=[ctx.pool.getconn() for _ in range(5)]
    try:
        assert ctx.service.ota_policy_for_bootstrap()=={**OTA,'updates_enabled':False}
        assert len(ctx.service.catalog_source()['decks'])==2
    finally:
        for connection in held:ctx.pool.putconn(connection)
    assert ctx.service.set_ota(bearer,1,OTA)['version']==2


def test_retention_none_preserves_old_audit_and_only_an_explicit_30_day_policy_purges_it(admin_pg):
    ctx=admin_pg;bearer=issued(ctx);old=uuid4()
    with ctx.pool.connection() as c:
        PostgresAdminRepository(c).audit({'id':old,'actor_owner_id':ctx.owner,'action':'fixture.old','target_type':'fixture',
            'target_id':'fixture','details':{},'created_at':NOW-timedelta(days=31)})
    ctx.service.purge_expired()
    with ctx.pool.connection() as c:assert c.execute('SELECT 1 FROM admin_audit WHERE id=%s',(old,)).fetchone()
    ctx.service.policy=replace(ctx.service.policy,audit_retention_days=30);ctx.service.purge_expired()
    with ctx.pool.connection() as c:assert c.execute('SELECT 1 FROM admin_audit WHERE id=%s',(old,)).fetchone() is None
    assert ctx.service.me(bearer)['audit_retention_days']==30


def test_a_request_waiting_on_an_owner_lock_observes_a_committed_admin_logout(admin_pg,monkeypatch):
    ctx=admin_pg;bearer=issued(ctx);observed=Event();original=PostgresAdminRepository.session
    def session(repo,key):
        result=original(repo,key);observed.set();return result
    monkeypatch.setattr(PostgresAdminRepository,'session',session)
    with ctx.pool.connection() as c, ThreadPoolExecutor(max_workers=1) as executor:
        lock_owner(c,ctx.owner)
        future=executor.submit(ctx.service.me,bearer)
        try:
            assert observed.wait(5)
            c.execute('DELETE FROM admin_sessions WHERE owner_id=%s',(ctx.owner,));c.commit()
            with pytest.raises(AdminError,match='ADMIN_SESSION_INVALID'):future.result(timeout=5)
        finally:c.rollback()


def test_admin_personal_export_is_owner_scoped_metadata_only_and_disappears_after_erasure(admin_pg):
    ctx=admin_pg;bearer=issued(ctx);foreign_normal=uuid4().hex+uuid4().hex;foreign_source=uuid4()
    with ctx.pool.connection() as c:
        c.execute('INSERT INTO social_accounts(owner_id) VALUES(%s)',(ctx.guest,))
        c.execute('''INSERT INTO social_device_sessions(id,owner_id,token_hash,platform,expires_at)
            VALUES(%s,%s,%s,'web',%s)''',(foreign_source,ctx.guest,digest(foreign_normal),NOW+timedelta(days=2)))
    ctx.manager.grant(ctx.guest,'viewer')
    foreign_grant=ctx.service.issue('Bearer '+foreign_normal)
    own_feedback,foreign_feedback=add_feedback(ctx,ctx.owner),add_feedback(ctx,ctx.guest)
    ctx.service.triage(bearer,own_feedback,0,'resolved')
    ctx.service.triage(bearer,foreign_feedback,0,'reviewing')
    with ctx.pool.connection() as c:
        exported=ctx.service.export_owner(c,ctx.owner)
    assert set(exported)=={'admin_roles','admin_sessions','admin_audit'}
    assert len(exported['admin_roles'])==1 and exported['admin_roles'][0]['role']=='publisher'
    assert set(exported['admin_roles'][0])=={'role','updated_at'}
    assert isinstance(exported['admin_roles'][0]['updated_at'],datetime)
    assert len(exported['admin_sessions'])==1
    session=exported['admin_sessions'][0]
    assert set(session)=={'id','role','expires_at','source_session_id'}
    assert session['source_session_id']==ctx.source and session['role']=='publisher'
    assert session['id']!=UUID(foreign_grant['session_id'])
    assert len(exported['admin_audit'])==3
    assert all(set(row)=={'id','action','target_type','target_id','created_at'} for row in exported['admin_audit'])
    feedback_targets=[row['target_id'] for row in exported['admin_audit'] if row['target_type']=='feedback']
    assert str(own_feedback) in feedback_targets and None in feedback_targets
    encoded=json.dumps(DataPrivacyUseCases._export_value(exported),sort_keys=True)
    for forbidden in ('token_hash','token','details','private fixture comment',str(ctx.guest),str(foreign_feedback),
                      str(foreign_source),foreign_grant['session_id'],ctx.normal,foreign_normal,
                      bearer.removeprefix('Bearer '),digest(bearer.removeprefix('Bearer '))):
        assert forbidden not in encoded
    with ctx.pool.connection() as c:
        lock_owner(c,ctx.owner);ctx.service.erase_owner(c,ctx.owner)
    with ctx.pool.connection() as c:
        assert ctx.service.export_owner(c,ctx.owner)=={'admin_roles':[],'admin_sessions':[],'admin_audit':[]}
        foreign=ctx.service.export_owner(c,ctx.guest)
        assert foreign['admin_roles'][0]['role']=='viewer' and len(foreign['admin_sessions'])==1
        assert len(foreign['admin_audit'])==1
