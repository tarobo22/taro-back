from datetime import datetime, timedelta
import hashlib
import re
import secrets
from typing import Callable
from uuid import UUID

from api.application.identity_ports import IdentityUnitOfWork
from api.domain.identity import IdentityPolicy, SessionGrant, SessionRejected, SessionRenewal


class IdentityUseCases:
    def __init__(self, uow: Callable[[], IdentityUnitOfWork], policy: IdentityPolicy,
                 clock: Callable[[], datetime], new_id: Callable[[], UUID]):
        self._uow, self.policy, self.clock, self.new_id = uow, policy, clock, new_id

    def create(self, platform: str, locale: str, timezone: str) -> SessionGrant:
        self.policy.validate(platform, locale, timezone)
        owner = self.new_id()
        token = secrets.token_hex(32)
        expires = self.clock() + timedelta(days=self.policy.session_days)
        with self._uow() as uow:
            uow.sessions.create(owner, hashlib.sha256(token.encode()).hexdigest(), expires,
                                platform, locale, timezone)
            uow.commit()
        return SessionGrant(owner, owner, token, expires)

    def authenticate(self, authorization: str | None) -> UUID:
        token = (authorization or '').removeprefix('Bearer ')
        if not re.fullmatch('[0-9a-f]{64}', token):
            raise SessionRejected('SESSION_INVALID')
        with self._uow() as uow:
            session = uow.sessions.find(hashlib.sha256(token.encode()).hexdigest())
        if session is None:
            raise SessionRejected('SESSION_INVALID')
        if session.expires_at <= self.clock():
            raise SessionRejected('SESSION_EXPIRED')
        return session.session_id

    def refresh(self, owner: UUID) -> SessionRenewal:
        now = self.clock()
        with self._uow() as uow:
            uow.sessions.lock(owner)
            session = uow.sessions.get(owner)
            if session is None or session.expires_at <= now:
                raise SessionRejected('SESSION_EXPIRED')
            expires = now + timedelta(days=self.policy.session_days)
            uow.sessions.renew(owner, expires)
            uow.commit()
        return SessionRenewal(owner, expires)
