from datetime import date
from typing import Protocol
from uuid import UUID

from api.domain.journal_summary import JournalMoodBucket


class JournalSummaryRepository(Protocol):
    def lock(self, owner: UUID) -> None: ...
    def active(self, owner: UUID) -> None: ...
    def groups(self, owner: UUID, start: date, end: date) -> list[JournalMoodBucket]: ...


class JournalSummaryUnitOfWork(Protocol):
    summary: JournalSummaryRepository

    def __enter__(self) -> 'JournalSummaryUnitOfWork': ...
    def __exit__(self, *exc: object) -> None: ...
