"""Non-AI operations over ports. Public queries can use a dedicated read pool."""
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
import secrets
from uuid import UUID, uuid4

from api.application.admin_ports import AdminUnitOfWork
from api.domain.admin import (AdminError, AdminPolicy, ota_values, page_input, page_result,
                              require_rights_evidence, revision, sha, snapshot_for)
from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.identity import SessionRejected


class AdminService:
    def __init__(self, uow_factory: Callable[[], AdminUnitOfWork], social_auth, catalog, *,
                 query_uow_factory=None, ota_defaults=None, policy=None, clock=None):
        self.uow_factory, self.social_auth = uow_factory, social_auth
        self.query_uow_factory = query_uow_factory or uow_factory
        self.source_catalog = deepcopy(catalog)
        self.ota_defaults = ota_values(ota_defaults or {'channel':'preview','min_native_version':'1.3.0','updates_enabled':False})
        self.policy = policy or AdminPolicy()
        if (self.policy.audit_retention_days is not None and
                (type(self.policy.audit_retention_days) is not int or self.policy.audit_retention_days <= 0)):
            raise ValueError('audit retention must be None or positive days')
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _normal_hash(authorization):
        token = (authorization or '').removeprefix('Bearer ')
        return sha(token) if re.fullmatch('[0-9a-f]{64}', token) else None

    def eligibility(self, authorization):
        try:
            owner = self.social_auth.authenticate(authorization)
        except SessionRejected:
            owner = None
        role = None
        if owner is not None:
            with self.uow_factory() as uow:
                uow.admin.lock_owner(owner)
                try:
                    uow.admin.active(owner)
                    role = uow.admin.role(owner) if uow.admin.member(owner) else None
                except (DataNotFound, OwnerDeleting):
                    role = None
        return {'eligible': role in ('viewer','publisher'), 'role': role, 'session_seconds':self.policy.session_seconds}

    def issue(self, authorization):
        try:
            owner = self.social_auth.authenticate(authorization)
        except SessionRejected:
            owner = None
        key = self._normal_hash(authorization)
        if owner is None or key is None:
            raise AdminError('ADMIN_FORBIDDEN', 403)
        now = self.clock()
        with self.uow_factory() as uow:
            repo = uow.admin
            repo.lock_owner(owner)
            repo.active(owner)
            role, source = repo.role(owner), repo.source_by_hash(key)
            if (role not in ('viewer','publisher') or not repo.member(owner) or not source
                    or source['owner_id'] != owner or source['revoked_at'] is not None or source['expires_at'] <= now):
                raise AdminError('ADMIN_FORBIDDEN', 403)
            token, session_id = 'admin_' + secrets.token_hex(32), uuid4()
            expires = min(now + timedelta(seconds=self.policy.session_seconds), source['expires_at'])
            repo.add_session({'id':session_id,'owner_id':owner,'token_hash':sha(token),
                'source_session_id':source['id'],'role':role,'expires_at':expires})
            self._audit(repo, owner, 'admin.session.issue', 'admin_session', session_id, {'role':role})
            uow.commit()
            return {'access_token':token,'session_id':str(session_id),'owner_id':str(owner),
                    'role':role,'expires_at':expires.isoformat()}

    def _principal(self, repo, authorization, *, write=False):
        token = (authorization or '').removeprefix('Bearer ')
        if not re.fullmatch('admin_[0-9a-f]{64}', token):
            raise AdminError('ADMIN_SESSION_INVALID', 401)
        row = repo.session(sha(token))
        if row is None:
            raise AdminError('ADMIN_SESSION_INVALID', 401)
        repo.lock_owner(row['owner_id'])
        # Logout/role revocation may have committed while this request waited
        # for the owner lock. A pre-lock session row cannot authorize it.
        row = repo.session(sha(token))
        if row is None:
            raise AdminError('ADMIN_SESSION_INVALID', 401)
        try:
            repo.active(row['owner_id'])
        except (DataNotFound, OwnerDeleting):
            raise AdminError('ADMIN_SESSION_INVALID', 401) from None
        if (row['expires_at'] <= self.clock() or repo.role(row['owner_id']) != row['role']
                or not repo.member(row['owner_id']) or not repo.source_valid(row['source_session_id'],row['owner_id'],self.clock())):
            raise AdminError('ADMIN_SESSION_INVALID', 401)
        if write and row['role'] != 'publisher':
            raise AdminError('ADMIN_FORBIDDEN', 403)
        return row

    def _audit(self, repo, owner, action, target_type, target_id, details):
        repo.audit({'id':uuid4(),'actor_owner_id':owner,'action':action,'target_type':target_type,
                    'target_id':str(target_id),'details':details,'created_at':self.clock()})

    def me(self, authorization):
        with self.uow_factory() as uow:
            row = self._principal(uow.admin, authorization)
            return {'owner_id':str(row['owner_id']),'role':row['role'],'expires_at':row['expires_at'].isoformat(),
                    'audit_retention_days':self.policy.audit_retention_days}

    def logout(self, authorization):
        with self.uow_factory() as uow:
            row = self._principal(uow.admin, authorization)
            uow.admin.delete_session(row['id'])
            self._audit(uow.admin,row['owner_id'],'admin.session.logout','admin_session',row['id'],{})
            uow.commit()

    def _versions(self, repo):
        versions = {str(row['version_id']):row for row in repo.versions_list()}
        for deck in self.source_catalog['decks']:
            snapshot, fingerprint = snapshot_for(self.source_catalog, deck['id'])
            row = versions.get(deck['id'])
            if row and row['snapshot_hash'] != fingerprint:
                raise AdminError('IMMUTABLE_CATALOG_VERSION',409)
            if row is None:
                versions[deck['id']] = {'version_id':deck['id'],'snapshot':snapshot,'snapshot_hash':fingerprint,
                    'rights_status':'unreviewed','review_note':'','review_version':0}
        return versions

    def _catalog_state(self, repo):
        versions = self._versions(repo)
        items = []
        for version_id,row in versions.items():
            snapshot = row['snapshot']; deck = snapshot['deck']; head = repo.head(deck['code'])
            publication = repo.publication(head['publication_id']) if head['publication_id'] else None
            items.append({'version_id':str(version_id),'code':deck['code'],'name':deck['name'],'version':deck['version'],
                'rights_status':row['rights_status'],'review_version':row['review_version'],'review_note':row['review_note'],
                'published':bool(publication and str(publication['version_id'])==str(version_id)),
                'publication_version':head['version'],'current_publication_id':str(head['publication_id']) if head['publication_id'] else None,
                'assets':[snapshot['deck']['back_asset'],*[card['image_asset'] for card in snapshot['cards']]]})
        publications = sorted(repo.publications_list(),key=lambda row:(row['created_at'],str(row['id'])),reverse=True)
        history = [{'id':str(row['id']),'version_id':str(row['version_id']),'code':row['code'],'action':row['action'],
            'previous_id':str(row['previous_id']) if row['previous_id'] else None,'created_at':row['created_at'].isoformat()} for row in publications]
        return {'items':items,'publications':history}

    def catalog(self, authorization):
        with self.uow_factory() as uow:
            self._principal(uow.admin,authorization)
            uow.admin.lock_governance(shared=True)
            return self._catalog_state(uow.admin)

    def review(self, authorization, version_id, expected, status, notes):
        revision(expected)
        if status not in ('approved','rejected') or not isinstance(notes,str) or not 1 <= len(notes.strip()) <= 1000:
            raise AdminError('REVIEW_INVALID')
        with self.uow_factory() as uow:
            repo=uow.admin; actor=self._principal(repo,authorization,write=True)
            repo.lock_governance()
            row=self._versions(repo).get(str(version_id))
            if row is None:raise AdminError('CATALOG_VERSION_NOT_FOUND',404)
            if row['review_version']!=expected:raise AdminError('STALE_REVIEW',409)
            if status=='approved':require_rights_evidence(row['snapshot'])
            previous=row['rights_status'];row={**row,'rights_status':status,'review_note':notes.strip(),'review_version':expected+1}
            repo.save_review(row)
            code=row['snapshot']['deck']['code'];head=repo.head(code)
            publication=repo.publication(head['publication_id']) if head['publication_id'] else None
            if status!='approved' and publication and str(publication['version_id'])==str(version_id):
                repo.save_head(code,None,head['version']+1)
                self._audit(repo,actor['owner_id'],'catalog.withdraw_on_review','catalog',version_id,{'from_publication':str(head['publication_id'])})
            self._audit(repo,actor['owner_id'],'catalog.review','catalog',version_id,{'from':previous,'to':status,'review_version':expected+1})
            result=self._catalog_state(repo);uow.commit();return result

    def _publish(self, repo, actor, row, head, *, action, snapshot=None):
        publication_id=uuid4();version_id=str(row['version_id']);code=row['snapshot']['deck']['code']
        repo.add_publication({'id':publication_id,'version_id':version_id,'code':code,'action':action,
            'previous_id':head['publication_id'],'snapshot':deepcopy(snapshot or row['snapshot']),'created_at':self.clock()})
        repo.save_head(code,publication_id,head['version']+1)
        self._audit(repo,actor['owner_id'],'catalog.'+action,'catalog',version_id,
                    {'publication_id':str(publication_id),'previous_id':str(head['publication_id']) if head['publication_id'] else None,
                     'publication_version':head['version']+1})

    def publish(self, authorization, version_id, expected_review, expected_publication):
        revision(expected_review);revision(expected_publication)
        with self.uow_factory() as uow:
            repo=uow.admin;actor=self._principal(repo,authorization,write=True);repo.lock_governance()
            row=self._versions(repo).get(str(version_id))
            if row is None:raise AdminError('CATALOG_VERSION_NOT_FOUND',404)
            if row['rights_status']!='approved':raise AdminError('CATALOG_NOT_APPROVED',409)
            require_rights_evidence(row['snapshot'])
            if row['review_version']!=expected_review:raise AdminError('STALE_REVIEW',409)
            head=repo.head(row['snapshot']['deck']['code'])
            if head['version']!=expected_publication:raise AdminError('STALE_PUBLICATION',409)
            self._publish(repo,actor,row,head,action='publish')
            result=self._catalog_state(repo);uow.commit();return result

    def rollback(self, authorization, version_id, publication_id, expected_publication):
        revision(expected_publication)
        with self.uow_factory() as uow:
            repo=uow.admin;actor=self._principal(repo,authorization,write=True);repo.lock_governance()
            row=self._versions(repo).get(str(version_id));target=repo.publication(publication_id)
            if row is None or target is None or str(target['version_id'])!=str(version_id):raise AdminError('PUBLICATION_NOT_FOUND',404)
            if row['rights_status']!='approved':raise AdminError('CATALOG_NOT_APPROVED',409)
            require_rights_evidence(target['snapshot'])
            head=repo.head(row['snapshot']['deck']['code'])
            if head['version']!=expected_publication:raise AdminError('STALE_PUBLICATION',409)
            self._publish(repo,actor,row,head,action='rollback',snapshot=target['snapshot'])
            result=self._catalog_state(repo);uow.commit();return result

    def catalog_source(self):
        with self.query_uow_factory() as uow:
            repo=uow.admin;repo.lock_governance(shared=True)
            versions=self._versions(repo);publications=repo.publications_list()
            published_before={str(row['version_id']) for row in publications}
            decks=[];by_deck={};default_cards=None
            for version_id,row in versions.items():
                snapshot=row['snapshot'];deck=deepcopy(snapshot['deck']);head=repo.head(deck['code'])
                current=repo.publication(head['publication_id']) if head['publication_id'] else None
                selected=bool(current and str(current['version_id'])==str(version_id))
                deck['status']='published' if selected else 'retired' if str(version_id) in published_before or row['rights_status']=='rejected' else 'candidate'
                decks.append(deck);by_deck[str(version_id)]=deepcopy(snapshot['cards'])
                if selected and default_cards is None:default_cards=by_deck[str(version_id)]
            fingerprint=sha(json.dumps({'decks':decks,'cards_by_deck':by_deck},sort_keys=True,separators=(',',':'),ensure_ascii=False))
            return {**deepcopy(self.source_catalog),'decks':decks,'cards':default_cards or deepcopy(self.source_catalog['cards']),
                    'cards_by_deck':by_deck,'source_manifest_sha256':fingerprint}

    def feedback(self, authorization, *, cursor=None, limit=None, status=None):
        if status not in (None,'new','reviewing','resolved','dismissed'):raise AdminError('FEEDBACK_STATUS_INVALID')
        with self.uow_factory() as uow:
            actor=self._principal(uow.admin,authorization)
            size,binding,after=page_input(self.policy,actor['owner_id'],'feedback',cursor,limit,{'status':status})
            return page_result(uow.admin.feedback_list(after=after,limit=size+1,status=status),size,binding)

    def triage(self, authorization, feedback_id, expected, status):
        revision(expected)
        if status not in ('new','reviewing','resolved','dismissed'):raise AdminError('FEEDBACK_STATUS_INVALID')
        with self.uow_factory() as uow:
            repo=uow.admin;actor=self._principal(repo,authorization,write=True)
            before=repo.feedback(feedback_id)
            if before is None:raise AdminError('FEEDBACK_NOT_FOUND',404)
            if not repo.triage(feedback_id,expected,status):raise AdminError('STALE_FEEDBACK',409)
            self._audit(repo,actor['owner_id'],'feedback.triage','feedback',feedback_id,{'from':before['triage_status'],'to':status,'version':expected+1})
            result=repo.feedback(feedback_id);uow.commit();return result

    def audits(self, authorization, *, cursor=None, limit=None):
        with self.uow_factory() as uow:
            actor=self._principal(uow.admin,authorization)
            size,binding,after=page_input(self.policy,actor['owner_id'],'audit',cursor,limit,{})
            return page_result(uow.admin.audits(after=after,limit=size+1),size,binding)

    def _ota(self,repo):
        return repo.ota_policy() or {**self.ota_defaults,'version':0}

    def get_ota(self,authorization):
        with self.uow_factory() as uow:
            self._principal(uow.admin,authorization);return self._ota(uow.admin)

    def set_ota(self,authorization,expected,values):
        revision(expected);values=ota_values(values)
        with self.uow_factory() as uow:
            repo=uow.admin;actor=self._principal(repo,authorization,write=True);repo.lock_governance()
            before=self._ota(repo)
            if before['version']!=expected:raise AdminError('STALE_OTA_POLICY',409)
            after={**values,'version':expected+1};repo.save_ota(after)
            self._audit(repo,actor['owner_id'],'ota.policy.update','ota_policy','global',{'from':before,'to':after})
            uow.commit();return after

    def ota_policy_for_bootstrap(self):
        with self.query_uow_factory() as uow:
            current=self._ota(uow.admin)
            return {key:current[key] for key in ('channel','min_native_version','updates_enabled')}

    def purge_expired(self):
        cutoff=None if self.policy.audit_retention_days is None else self.clock()-timedelta(days=self.policy.audit_retention_days)
        with self.uow_factory() as uow:
            uow.admin.purge(self.clock(),cutoff);uow.commit()

    def erase_owner(self,transaction,owner):
        self.uow_factory().repository_for(transaction).erase_owner(owner)

    def export_owner(self,transaction,owner):
        return self.uow_factory().repository_for(transaction).export_owner(owner)


class AdminRoleManager:
    """Local operator capability. This class is never exposed by an HTTP route."""
    def __init__(self,uow_factory,*,clock=None):
        self.uow_factory=uow_factory;self.clock=clock or (lambda:datetime.now(timezone.utc))

    def grant(self,owner,role):
        owner=UUID(str(owner))
        if role not in ('viewer','publisher'):raise AdminError('ADMIN_ROLE_INVALID')
        with self.uow_factory() as uow:
            repo=uow.admin;repo.lock_owner(owner);repo.active(owner)
            if not repo.member(owner):raise AdminError('MEMBER_REQUIRED',409)
            repo.set_role(owner,role);repo.delete_owner_sessions(owner)
            repo.audit({'id':uuid4(),'actor_owner_id':None,'action':'admin.role.grant','target_type':'admin_role',
                'target_id':str(owner),'details':{'role':role,'source':'explicit-local-cli'},'created_at':self.clock()})
            uow.commit()

    def revoke(self,owner):
        owner=UUID(str(owner))
        with self.uow_factory() as uow:
            repo=uow.admin;repo.lock_owner(owner);repo.revoke_role(owner);repo.delete_owner_sessions(owner)
            repo.audit({'id':uuid4(),'actor_owner_id':None,'action':'admin.role.revoke','target_type':'admin_role',
                'target_id':str(owner),'details':{'source':'explicit-local-cli'},'created_at':self.clock()})
            uow.commit()
