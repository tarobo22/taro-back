"""Journal aggregate and externally supplied policy."""
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID


class JournalRuleViolation(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class JournalRequestConflict(JournalRuleViolation):
    """One client request identity must describe one logical create."""


class JournalVersionConflict(JournalRuleViolation):
    """An editor cannot replace a version it has not read."""


@dataclass(frozen=True)
class JournalPolicy:
    moods: frozenset[str]
    max_body_length: int


@dataclass(frozen=True)
class JournalEntry:
    id: UUID
    owner_id: UUID
    day: date
    mood: str
    body: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    version: int = 1

    @classmethod
    def create(cls, *, entry_id: UUID, owner_id: UUID, day: date,
               mood: str, body: str, policy: JournalPolicy) -> 'JournalEntry':
        if not isinstance(day, date):
            raise JournalRuleViolation('invalid_day')
        if mood not in policy.moods:
            raise JournalRuleViolation('invalid_mood')
        if len(body) > policy.max_body_length:
            raise JournalRuleViolation('body_too_long')
        return cls(entry_id, owner_id, day, mood, body.strip())
