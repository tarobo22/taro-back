"""Account/session use cases; provider I/O never runs in a database transaction."""
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hmac
import re
import secrets
from urllib.parse import urlencode
from uuid import UUID, uuid4

from api.application.social_auth_ports import SocialProvider, SocialUnitOfWork
from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.identity import SessionRejected
from api.domain.social_auth import PROVIDERS, SocialAuthError, SocialAuthPolicy, digest, pkce_challenge, valid_opaque


class SocialAuthService:
    def __init__(self, uow_factory: Callable[[], SocialUnitOfWork], provider: SocialProvider,
                 policy: SocialAuthPolicy, *, clock=None):
        self.uow_factory, self.provider, self.policy = uow_factory, provider, policy
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def start(self, owner, provider, return_uri, challenge, platform, *, intent='sign_in'):
        self.policy.validate_start(provider, return_uri, challenge, platform, intent)
        if not self.provider.available(provider):
            raise SocialAuthError('PROVIDER_UNAVAILABLE', 503)
        state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        now = self.clock()
        expires = now + timedelta(seconds=self.policy.flow_seconds)
        url = self.provider.authorization_url(provider, state=state, nonce=nonce, verifier=verifier)
        with self.uow_factory() as uow:
            repo = uow.social
            repo.lock_owner(owner)
            repo.active(owner)
            repo.purge(now)
            if repo.flow_count(owner, now) >= self.policy.max_flows_per_owner:
                raise SocialAuthError('AUTH_FLOW_LIMIT', 429)
            repo.add_flow({'id': uuid4(), 'owner_id': owner, 'provider': provider, 'state_hash': digest(state),
                'nonce': digest(nonce), 'provider_verifier': verifier, 'client_challenge': challenge,
                'return_uri': return_uri, 'platform': platform, 'intent': intent, 'state': 'pending', 'expires_at': expires})
            uow.commit()
        return {'authorization_url': url, 'expires_at': expires.isoformat()}

    def providers(self):
        return {'providers': [{'provider': provider, 'available': bool(self.provider.available(provider)),
                 'reason': None if self.provider.available(provider) else 'provider_not_configured'} for provider in PROVIDERS]}

    def _failed_callback(self, flow, error='authentication_failed'):
        with self.uow_factory() as uow:
            uow.social.lock_owner(flow['owner_id'])
            uow.social.discard_flow(flow['state_hash'])
            uow.commit()
        return flow['return_uri'] + '#' + urlencode({'auth_error': error})

    def callback(self, provider, state, *, code=None, error=None):
        if provider not in PROVIDERS:
            raise SocialAuthError('AUTH_FLOW_INVALID', 400)
        state_hash = digest(valid_opaque(state))
        with self.uow_factory() as uow:
            flow = uow.social.flow(state_hash)
            if not flow or flow['provider'] != provider:
                raise SocialAuthError('AUTH_FLOW_INVALID', 400)
            uow.social.lock_owner(flow['owner_id'])
            try:
                uow.social.active(flow['owner_id'])
            except (DataNotFound, OwnerDeleting):
                uow.social.discard_flow(state_hash)
                uow.commit()
                return flow['return_uri'] + '#auth_error=authentication_failed'
            if flow['expires_at'] <= self.clock():
                uow.social.discard_flow(state_hash)
                uow.commit()
                return flow['return_uri'] + '#auth_error=flow_expired'
            flow = uow.social.claim_flow(state_hash, provider, self.clock())
            if flow is None:
                raise SocialAuthError('AUTH_FLOW_INVALID', 400)
            # Reserve state once, then release all locks before external I/O.
            uow.commit()
        if error is not None or not isinstance(code, str) or not 1 <= len(code) <= 8192:
            return self._failed_callback(flow, 'cancelled' if error == 'access_denied' else 'authentication_failed')
        try:
            try:
                identity = self.provider.exchange(provider, code, nonce_hash=flow['nonce'], verifier=flow['provider_verifier'])
            except Exception:
                # A provider or runtime credential-source failure must complete
                # the browser handoff without exposing provider error text.
                return self._failed_callback(flow)
            if identity.provider != provider or not re.fullmatch('[0-9a-f]{64}', identity.subject_hash):
                raise SocialAuthError('PROVIDER_IDENTITY_INVALID', 401)
            ticket = secrets.token_urlsafe(32)
            with self.uow_factory() as uow:
                uow.social.lock_owner(flow['owner_id'])
                uow.social.active(flow['owner_id'])
                if flow['expires_at'] <= self.clock() or not uow.social.complete_flow(state_hash, identity.subject_hash,
                        digest(ticket), min(flow['expires_at'], self.clock() + timedelta(seconds=self.policy.ticket_seconds))):
                    raise SocialAuthError('AUTH_FLOW_INVALID', 400)
                uow.commit()
            return flow['return_uri'] + '#' + urlencode({'auth_ticket': ticket, 'auth_provider': provider})
        except (SocialAuthError, DataNotFound, OwnerDeleting):
            return self._failed_callback(flow)

    def exchange(self, owner, ticket, verifier):
        ticket_hash, challenge = digest(valid_opaque(ticket)), pkce_challenge(verifier)
        now = self.clock()
        with self.uow_factory() as uow:
            repo = uow.social
            flow = repo.ticket(owner, ticket_hash, now)
            if flow is None:
                raise SocialAuthError('AUTH_TICKET_INVALID', 401)
            # A short global account-admission lock precedes owner locks. This also
            # orders simultaneous links between two owners without a lock cycle.
            repo.lock_identity(flow['provider'], flow['subject_hash'])
            repo.lock_owner(owner)
            repo.active(owner)
            flow = repo.ticket(owner, ticket_hash, self.clock())
            if flow is None or not hmac.compare_digest(flow['client_challenge'], challenge):
                raise SocialAuthError('AUTH_TICKET_INVALID', 401)
            target = repo.identity_owner(flow['provider'], flow['subject_hash'])
            if flow['intent'] == 'link' and target is not None and target != owner:
                raise SocialAuthError('ACCOUNT_ALREADY_LINKED', 409)
            if target is None:
                if repo.member(owner) and flow['intent'] != 'link':
                    raise SocialAuthError('ACCOUNT_LINK_REQUIRED', 409)
                if repo.has_provider(owner, flow['provider']):
                    raise SocialAuthError('ACCOUNT_PROVIDER_EXISTS', 409)
                target = owner
                repo.link_identity(owner, flow['provider'], flow['subject_hash'])
                repo.capture_legacy_session(owner)
            if target != owner:
                repo.lock_owner(target)
                repo.active(target)
            token, session_id = secrets.token_hex(32), uuid4()
            expires = now + timedelta(days=self.policy.session_days)
            repo.add_session({'id': session_id, 'owner_id': target, 'token_hash': digest(token),
                'platform': flow['platform'], 'created_at': now, 'expires_at': expires, 'revoked_at': None})
            repo.discard_flow(flow['state_hash'])
            uow.commit()
        return {'user_id': str(target), 'session_id': str(session_id), 'access_token': token,
                'expires_at': expires.isoformat(), 'previous_user_id': str(owner), 'guest_records_preserved': True}

    @staticmethod
    def _token_hash(authorization):
        token = (authorization or '').removeprefix('Bearer ')
        return digest(token) if re.fullmatch('[0-9a-f]{64}', token) else None

    def _session(self, repo, authorization):
        key = self._token_hash(authorization)
        row = repo.session_by_hash(key) if key else None
        if row is None:
            return None
        repo.lock_owner(row['owner_id'])
        row = repo.session_by_hash(key)
        if row is None or row['revoked_at'] is not None or row['expires_at'] <= self.clock():
            raise SessionRejected('SESSION_EXPIRED_OR_REVOKED')
        try:
            repo.active(row['owner_id'])
        except (DataNotFound, OwnerDeleting):
            raise SessionRejected('SESSION_INVALID') from None
        return row

    def authenticate(self, authorization):
        with self.uow_factory() as uow:
            row = self._session(uow.social, authorization)
            return row['owner_id'] if row else None

    def refresh(self, authorization):
        with self.uow_factory() as uow:
            row = self._session(uow.social, authorization)
            if row is None:
                return None
            expires = self.clock() + timedelta(days=self.policy.session_days)
            uow.social.renew(row['id'], expires)
            uow.commit()
            return {'session_id': str(row['id']), 'expires_at': expires.isoformat()}

    def account(self, owner):
        with self.uow_factory() as uow:
            uow.social.lock_owner(owner)
            uow.social.active(owner)
            return {'user_id': str(owner), 'kind': 'member' if uow.social.member(owner) else 'guest',
                    'providers': uow.social.providers(owner)}

    def sessions(self, owner, authorization):
        with self.uow_factory() as uow:
            uow.social.lock_owner(owner)
            uow.social.active(owner)
            token_hash = self._token_hash(authorization)
            return {'items': [{'id': str(row['id']), 'platform': row['platform'],
                'created_at': row['created_at'].isoformat(), 'expires_at': row['expires_at'].isoformat(),
                'current': row['token_hash'] == token_hash} for row in uow.social.list_sessions(owner)
                if row['revoked_at'] is None and row['expires_at'] > self.clock()]}

    def logout(self, authorization):
        with self.uow_factory() as uow:
            row = self._session(uow.social, authorization)
            if row is None:
                raise SessionRejected('SOCIAL_SESSION_REQUIRED')
            uow.social.revoke(row['owner_id'], row['id'], self.clock())
            uow.commit()

    def revoke_session(self, owner, session_id):
        try:
            session_id = UUID(str(session_id))
        except (ValueError, TypeError):
            raise SocialAuthError('SESSION_ID_INVALID') from None
        with self.uow_factory() as uow:
            uow.social.lock_owner(owner)
            uow.social.active(owner)
            if not uow.social.revoke(owner, session_id, self.clock()):
                raise SocialAuthError('SESSION_NOT_FOUND', 404)
            uow.commit()

    def logout_all(self, owner):
        with self.uow_factory() as uow:
            uow.social.lock_owner(owner)
            uow.social.active(owner)
            uow.social.revoke_all(owner, self.clock())
            uow.commit()

    def owner_kind(self, transaction, owner):
        return 'member' if self.uow_factory().repository_for(transaction).member(owner) else 'guest'

    def export_owner(self, transaction, owner):
        return self.uow_factory().repository_for(transaction).export_owner(owner)

    def erase_owner(self, transaction, owner):
        self.uow_factory().repository_for(transaction).erase_owner(owner)

    def purge_expired(self):
        with self.uow_factory() as uow:
            uow.social.purge(self.clock())
            uow.commit()
