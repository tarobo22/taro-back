from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class IdentityRuleViolation(ValueError):
    pass


class SessionRejected(ValueError):
    pass


@dataclass(frozen=True)
class IdentityPolicy:
    session_days: int
    locales: tuple[str, ...]

    def validate(self, platform: str, locale: str, timezone: str) -> None:
        if platform not in ('web', 'android', 'ios') or locale not in self.locales:
            raise IdentityRuleViolation('unsupported_device_or_locale')
        try:
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise IdentityRuleViolation('invalid_timezone') from None


@dataclass(frozen=True)
class DeviceSession:
    session_id: UUID
    expires_at: datetime


@dataclass(frozen=True)
class SessionGrant:
    user_id: UUID
    session_id: UUID
    access_token: str = field(repr=False)
    expires_at: datetime


@dataclass(frozen=True)
class SessionRenewal:
    session_id: UUID
    expires_at: datetime
