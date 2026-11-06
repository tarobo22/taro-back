from typing import Any, Protocol
from uuid import UUID


class JournalSourceExists(Protocol):
    def __call__(self, connection: Any, owner: UUID, kind: str, source_id: UUID) -> bool: ...


class JournalSourceLookup(Protocol):
    def __call__(self, connection: Any, owner: UUID, kind: str, source_id: UUID) -> dict | None: ...


class InvalidateJournalContext(Protocol):
    def __call__(self, connection: Any, owner: UUID, entry_id: UUID) -> None: ...


class JournalV2UnitOfWork(Protocol):
    journals: Any
    connection: Any

    def repository_for(self, connection: Any) -> Any: ...
    def __enter__(self) -> 'JournalV2UnitOfWork': ...
    def __exit__(self, *exc: object) -> None: ...
    def commit(self) -> None: ...
