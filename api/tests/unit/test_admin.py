from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock
from uuid import uuid4

import pytest

from api.application.admin import AdminRoleManager, AdminService
from api.domain.admin import AdminError, AdminPolicy
from api.domain.social_auth import digest
from api.tests.support.admin_fake import MemoryAdminRepository, MemoryAdminUnit, catalog_fixture


NOW=datetime(2026,11,6,tzinfo=timezone.utc)
OTA={'channel':'preview','min_native_version':'1.3.0','updates_enabled':True}


def setup(role=None,retention=None):
    repo=MemoryAdminRepository();owner=uuid4();source=uuid4();token='a'*64
    repo.owners.add(owner);repo.members.add(owner)
    repo.sources[digest(token)]={'id':source,'owner_id':owner,'expires_at':NOW+timedelta(days=2),'revoked_at':None}
    if role:repo.roles[owner]=role
    auth=Mock();auth.authenticate.return_value=owner
    catalog,ids=catalog_fixture();clock=[NOW];factory=Mock(return_value=MemoryAdminUnit(repo))
    service=AdminService(factory,auth,catalog,ota_defaults=OTA,policy=AdminPolicy(audit_retention_days=retention),clock=lambda:clock[0])
    return service,repo,owner,token,catalog,ids,clock,factory


def issued(role='publisher',retention=None):
    service,repo,owner,token,catalog,ids,clock,factory=setup(role,retention)
    grant=service.issue('Bearer '+token)
    return service,repo,owner,grant['access_token'],catalog,ids,clock,factory


def test_default_zero_roles_cannot_mint_an_admin_bearer():
    service,repo,_,token,_,_,_,_=setup()
    with pytest.raises(AdminError,match='ADMIN_FORBIDDEN'):service.issue('Bearer '+token)
    assert not repo.sessions


def test_explicit_role_manager_only_grants_existing_social_members_and_revokes_admin_sessions():
    service,repo,owner,token,_,_,_,factory=setup()
    manager=AdminRoleManager(factory,clock=lambda:NOW)
    repo.members.remove(owner)
    with pytest.raises(AdminError,match='MEMBER_REQUIRED'):manager.grant(owner,'publisher')
    assert not repo.roles
    repo.members.add(owner);manager.grant(owner,'viewer')
    admin=service.issue('Bearer '+token)['access_token'];manager.revoke(owner)
    with pytest.raises(AdminError,match='ADMIN_SESSION_INVALID'):service.me('Bearer '+admin)
    assert not repo.roles and repo.audit_rows[-1]['action']=='admin.role.revoke'


def test_admin_session_is_rechecked_after_owner_lock_so_concurrent_logout_cannot_resurrect_it():
    service,repo,_,token,_,_,_,_=issued()
    original=repo.session;calls=[]
    def session(key):
        calls.append(key)
        return original(key) if len(calls)==1 else None
    repo.session=session
    with pytest.raises(AdminError,match='ADMIN_SESSION_INVALID'):service.me('Bearer '+token)


def test_incomplete_rights_evidence_remains_reviewable_but_cannot_be_approved_or_published():
    service,repo,_,token,_,ids,_,_=issued()
    service.source_catalog['cards_by_deck'][ids[0]][0]['image_asset']['source_page']=None
    assert len(service.catalog('Bearer '+token)['items'])==2
    with pytest.raises(AdminError,match='CATALOG_ASSET_EVIDENCE_MISSING'):
        service.review('Bearer '+token,ids[0],0,'approved','fixture evidence missing')
    assert not repo.versions
    rejected=service.review('Bearer '+token,ids[0],0,'rejected','fixture evidence is missing')
    assert next(row for row in rejected['items'] if row['version_id']==ids[0])['rights_status']=='rejected'
    with pytest.raises(AdminError,match='CATALOG_NOT_APPROVED'):
        service.publish('Bearer '+token,ids[0],1,0)


def test_valid_role_issues_a_short_separate_token_and_status_never_contains_source_token():
    service,repo,owner,token,_,_,_,_=issued('viewer')
    assert token.startswith('admin_') and len(token)==70
    assert token not in repr(repo.sessions)
    me=service.me('Bearer '+token)
    assert me['owner_id']==str(owner) and me['role']=='viewer' and me['audit_retention_days'] is None
    assert set(me)=={'owner_id','role','expires_at','audit_retention_days'}


def test_viewer_can_read_but_cannot_review_publish_triage_or_change_ota():
    service,repo,_,token,_,ids,_,_=issued('viewer')
    assert len(service.catalog('Bearer '+token)['items'])==2
    assert service.get_ota('Bearer '+token)=={**OTA,'version':0}
    for action in (lambda:service.review('Bearer '+token,ids[0],0,'approved','fixture rights checked'),
                   lambda:service.publish('Bearer '+token,ids[0],0,0),
                   lambda:service.set_ota('Bearer '+token,0,{**OTA,'updates_enabled':False})):
        with pytest.raises(AdminError,match='ADMIN_FORBIDDEN'):action()
    assert not repo.versions and repo.ota is None


def test_unreviewed_catalog_cannot_publish_and_stale_review_is_a_conflict():
    service,repo,_,token,_,ids,_,_=issued()
    with pytest.raises(AdminError,match='CATALOG_NOT_APPROVED'):service.publish('Bearer '+token,ids[0],0,0)
    result=service.review('Bearer '+token,ids[0],0,'approved','fixture evidence reviewed')
    assert next(row for row in result['items'] if row['version_id']==ids[0])['review_version']==1
    with pytest.raises(AdminError,match='STALE_REVIEW'):service.review('Bearer '+token,ids[0],0,'rejected','old editor')
    assert repo.version(ids[0])['rights_status']=='approved'


def test_publish_and_explicit_previous_snapshot_rollback_preserve_immutable_cards_and_ids():
    service,repo,_,token,catalog,ids,_,_=issued()
    original=deepcopy(catalog)
    service.review('Bearer '+token,ids[0],0,'approved','first fixture reviewed')
    first=service.publish('Bearer '+token,ids[0],1,0)
    first_id=first['publications'][0]['id']
    service.review('Bearer '+token,ids[1],0,'approved','second fixture reviewed')
    service.publish('Bearer '+token,ids[1],1,1)
    assert next(row for row in service.catalog('Bearer '+token)['items'] if row['version_id']==ids[1])['published']
    result=service.rollback('Bearer '+token,ids[0],first_id,2)
    assert next(row for row in result['items'] if row['version_id']==ids[0])['published']
    view=service.catalog_source()
    assert next(deck for deck in view['decks'] if deck['id']==ids[0])['status']=='published'
    assert view['cards_by_deck'][ids[0]]==original['cards_by_deck'][ids[0]]
    assert catalog==original
    assert repo.audit_rows[-1]['action']=='catalog.rollback'
    with pytest.raises(AdminError):service.rollback('Bearer '+token,ids[0],str(uuid4()),3)
    with pytest.raises(AdminError):service.publish('Bearer '+token,ids[1],1,0)


def test_review_demotion_withdraws_the_current_head_and_never_silently_republishes_on_reapproval():
    service,repo,_,token,_,ids,_,_=issued()
    service.review('Bearer '+token,ids[0],0,'approved','fixture reviewed')
    service.publish('Bearer '+token,ids[0],1,0)
    service.review('Bearer '+token,ids[0],1,'rejected','fixture evidence withdrawn')
    assert not next(row for row in service.catalog('Bearer '+token)['items'] if row['version_id']==ids[0])['published']
    service.review('Bearer '+token,ids[0],2,'approved','new fixture review')
    assert not next(row for row in service.catalog('Bearer '+token)['items'] if row['version_id']==ids[0])['published']


def test_source_session_revocation_role_change_owner_erasure_and_ttl_reject_admin_token():
    for change in ('source','role','owner','ttl'):
        service,repo,owner,token,_,_,clock,_=issued()
        if change=='source':next(iter(repo.sources.values()))['revoked_at']=NOW
        if change=='role':repo.roles[owner]='viewer'
        if change=='owner':repo.owners.remove(owner)
        if change=='ttl':clock[0]+=timedelta(minutes=16)
        with pytest.raises(AdminError):service.me('Bearer '+token)


def test_ota_cas_pause_resume_is_metadata_only_and_audited():
    service,repo,_,token,_,_,_,_=issued()
    assert service.set_ota('Bearer '+token,0,{**OTA,'updates_enabled':False})=={**OTA,'updates_enabled':False,'version':1}
    assert service.ota_policy_for_bootstrap()=={**OTA,'updates_enabled':False}
    with pytest.raises(AdminError,match='STALE_OTA_POLICY'):service.set_ota('Bearer '+token,0,OTA)
    assert service.set_ota('Bearer '+token,1,OTA)=={**OTA,'version':2}
    assert repo.audit_rows[-1]['action']=='ota.policy.update'


def test_unspecified_audit_retention_never_deletes_automatically_and_explicit_policy_can_purge():
    service,repo,owner,_,_,_,clock,_=issued(retention=None)
    clock[0]+=timedelta(days=100)
    service.purge_expired()
    assert repo.audit_rows
    service,repo,owner,_,_,_,clock,_=issued(retention=30)
    clock[0]+=timedelta(days=31)
    service.purge_expired()
    assert repo.audit_rows==[]


def test_constructor_is_database_free_and_public_catalog_query_errors_propagate():
    service,repo,_,_,_,_,_,factory=setup('publisher')
    factory.assert_not_called()
    factory.side_effect=RuntimeError('fixture database unavailable')
    with pytest.raises(RuntimeError,match='database unavailable'):service.catalog_source()
