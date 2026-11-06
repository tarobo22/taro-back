from copy import deepcopy
from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import uuid4

from api.domain.social_auth import digest


class MemoryAdminRepository:
    def __init__(self):
        self.owners, self.members, self.roles, self.sources, self.sessions = set(), set(), {}, {}, {}
        self.versions, self.heads, self.publications, self.feedback_rows, self.audit_rows = {}, {}, {}, {}, []
        self.ota = None

    def lock_owner(self, owner): pass
    def lock_governance(self, *, shared=False): pass
    def active(self, owner):
        from api.domain.data_privacy import DataNotFound
        if owner not in self.owners: raise DataNotFound('owner')
    def member(self, owner): return owner in self.members
    def role(self, owner): return self.roles.get(owner)
    def set_role(self, owner, role): self.roles[owner] = role
    def revoke_role(self, owner): self.roles.pop(owner, None)
    def source_by_hash(self, key): return deepcopy(self.sources.get(key))
    def source_valid(self, session, owner, now):
        return any(row['id'] == session and row['owner_id'] == owner and row['expires_at'] > now and row['revoked_at'] is None for row in self.sources.values())
    def add_session(self, row): self.sessions[row['token_hash']] = deepcopy(row)
    def session(self, key): return deepcopy(self.sessions.get(key))
    def delete_session(self, session_id):
        self.sessions = {key: row for key, row in self.sessions.items() if row['id'] != session_id}
    def delete_owner_sessions(self, owner):
        self.sessions = {key: row for key, row in self.sessions.items() if row['owner_id'] != owner}
    def versions_list(self): return deepcopy(list(self.versions.values()))
    def version(self, version_id): return deepcopy(self.versions.get(str(version_id)))
    def save_review(self, row): self.versions[str(row['version_id'])] = deepcopy(row)
    def head(self, code): return deepcopy(self.heads.get(code, {'version': 0, 'publication_id': None}))
    def save_head(self, code, publication_id, version): self.heads[code] = {'version': version, 'publication_id': publication_id}
    def add_publication(self, row): self.publications[str(row['id'])] = deepcopy(row)
    def publication(self, publication_id): return deepcopy(self.publications.get(str(publication_id)))
    def publications_list(self): return deepcopy(list(self.publications.values()))
    def feedback(self, feedback_id): return deepcopy(self.feedback_rows.get(str(feedback_id)))
    def feedback_list(self, *, after=None, limit=20, status=None):
        rows = sorted(self.feedback_rows.values(), key=lambda row:(row['created_at'],str(row['id'])), reverse=True)
        return deepcopy([row for row in rows if status is None or row['triage_status'] == status][:limit])
    def triage(self, feedback_id, expected, status):
        row = self.feedback_rows.get(str(feedback_id))
        if row is None or row['triage_version'] != expected: return False
        row.update(triage_version=expected+1, triage_status=status); return True
    def audit(self, row): self.audit_rows.append(deepcopy(row))
    def audits(self, *, after=None, limit=20): return deepcopy(self.audit_rows[::-1][:limit])
    def ota_policy(self): return deepcopy(self.ota)
    def save_ota(self, values): self.ota = deepcopy(values)
    def purge(self, now, cutoff):
        self.sessions = {key: row for key, row in self.sessions.items() if row['expires_at'] > now}
        if cutoff is not None: self.audit_rows = [row for row in self.audit_rows if row['created_at'] > cutoff]
    def erase_owner(self, owner):
        self.revoke_role(owner); self.delete_owner_sessions(owner)
        for row in self.audit_rows:
            if row['actor_owner_id'] == owner: row['actor_owner_id'] = None


class MemoryAdminUnit:
    def __init__(self, repository): self.admin = repository
    def __enter__(self): self.before = deepcopy(vars(self.admin)); return self
    def commit(self): self.before = deepcopy(vars(self.admin))
    def __exit__(self,*exc):
        if exc[0] is not None: vars(self.admin).update(self.before)
    def repository_for(self, tx): return self.admin


def catalog_fixture():
    def asset(n):
        return {'id': str(uuid4()), 'url': 'https://assets.example.test/'+str(n), 'sha256': 'a'*64,
                'source_page': 'https://source.example.test/'+str(n), 'license_name': 'Fixture public license',
                'license_url': 'https://license.example.test/', 'width':500,'height':800}
    ids = [str(uuid4()), str(uuid4())]
    decks, by_deck = [], {}
    for index, version in enumerate(ids):
        decks.append({'id':version,'code':'fixture-deck','name':'Fixture deck','version':str(index+1),
                      'card_count':78,'status':'candidate','back_asset':asset('back'+str(index))})
        by_deck[version] = [{'id':n,'code':'card_'+str(n),'name_ko':'카드'+str(n),'name_en':'Card',
            'arcana':'major' if n<22 else 'minor','suit':None if n<22 else 'cups',
            'basic_meaning':'Fixture meaning','image_asset':asset(str(index)+'-'+str(n))} for n in range(78)]
    return {'decks':decks,'cards':by_deck[ids[0]],'cards_by_deck':by_deck,'source_manifest_sha256':'b'*64}, ids
