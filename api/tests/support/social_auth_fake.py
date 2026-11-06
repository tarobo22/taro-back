from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4


class MemorySocialRepository:
    """Transaction fake has no SQL/connection API and never contacts a provider."""
    def __init__(self):
        self.active_owners = set()
        self.flows, self.sessions, self.identities, self.accounts = {}, {}, {}, set()
        self.locks, self.events = [], []

    def lock_owner(self, owner):
        self.locks.append(owner)

    def active(self, owner):
        from api.domain.data_privacy import DataNotFound
        if owner not in self.active_owners:
            raise DataNotFound('owner_not_found')

    def purge(self, now):
        self.flows = {key: row for key, row in self.flows.items() if row['expires_at'] > now}

    def flow_count(self, owner, now):
        return sum(row['owner_id'] == owner and row['expires_at'] > now for row in self.flows.values())

    def add_flow(self, row):
        self.flows[row['state_hash']] = dict(row)

    def flow(self, state_hash):
        return deepcopy(self.flows.get(state_hash))

    def claim_flow(self, state_hash, provider, now):
        row = self.flows.get(state_hash)
        if not row or row['provider'] != provider or row['state'] != 'pending' or row['expires_at'] <= now:
            return None
        row['state'] = 'processing'
        return deepcopy(row)

    def complete_flow(self, state_hash, subject_hash, ticket_hash, expires_at):
        row = self.flows.get(state_hash)
        if not row or row['state'] != 'processing':
            return False
        row.update(state='ready', subject_hash=subject_hash, ticket_hash=ticket_hash,
                   nonce=None, provider_verifier=None, expires_at=expires_at)
        return True

    def discard_flow(self, state_hash):
        self.flows.pop(state_hash, None)

    def ticket(self, owner, ticket_hash, now):
        return next((deepcopy(row) for row in self.flows.values() if row['owner_id'] == owner and
                     row.get('ticket_hash') == ticket_hash and row['state'] == 'ready' and row['expires_at'] > now), None)

    def identity_owner(self, provider, subject_hash):
        return self.identities.get((provider, subject_hash))

    def lock_identity(self, provider, subject_hash):
        self.events.append('identity-lock')

    def member(self, owner):
        return owner in self.accounts

    def link_identity(self, owner, provider, subject_hash):
        self.accounts.add(owner)
        self.identities[(provider, subject_hash)] = owner

    def has_provider(self, owner, provider):
        return any(p == provider and value == owner for (p, _), value in self.identities.items())

    def providers(self, owner):
        return sorted(p for (p, _), value in self.identities.items() if value == owner)

    def capture_legacy_session(self, owner):
        self.events.append(('capture-legacy', owner))

    def add_session(self, row):
        self.sessions[row['id']] = dict(row)

    def session_by_hash(self, token_hash):
        return next((deepcopy(row) for row in self.sessions.values() if row['token_hash'] == token_hash), None)

    def renew(self, session_id, expires_at):
        self.sessions[session_id]['expires_at'] = expires_at

    def list_sessions(self, owner):
        return [deepcopy(row) for row in self.sessions.values() if row['owner_id'] == owner]

    def revoke(self, owner, session_id, now):
        row = self.sessions.get(session_id)
        if row is None or row['owner_id'] != owner:
            return False
        row['revoked_at'] = now
        return True

    def revoke_all(self, owner, now):
        for row in self.sessions.values():
            if row['owner_id'] == owner:
                row['revoked_at'] = now

    def export_owner(self, owner):
        return {'social_accounts': [{'provider': p} for p in self.providers(owner)]}

    def erase_owner(self, owner):
        self.accounts.discard(owner)
        self.identities = {key: value for key, value in self.identities.items() if value != owner}
        self.sessions = {key: row for key, row in self.sessions.items() if row['owner_id'] != owner}
        self.flows = {key: row for key, row in self.flows.items() if row['owner_id'] != owner}


class MemorySocialUnit:
    def __init__(self, repository):
        self.social = repository
        self.commits = 0

    def __enter__(self):
        self.before = deepcopy(vars(self.social))
        return self

    def __exit__(self, *exc):
        if exc[0] is not None:
            vars(self.social).update(self.before)

    def commit(self):
        self.commits += 1
        self.before = deepcopy(vars(self.social))

    def repository_for(self, transaction):
        assert transaction == 'opaque-test-transaction'
        return self.social
