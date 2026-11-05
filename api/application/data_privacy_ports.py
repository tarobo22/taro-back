"""Transaction callbacks keep privacy orchestration independent of AI/reading SQL."""
from typing import Any, Callable, Protocol
from uuid import UUID


class ExportOwner(Protocol):
    def __call__(self, connection: Any, owner: UUID) -> dict: ...


class EraseOwner(Protocol):
    def __call__(self, connection: Any, owner: UUID) -> None: ...


class FeedbackTargetExists(Protocol):
    def __call__(self, connection: Any, owner: UUID, kind: str, target: UUID) -> bool: ...


class DataPrivacyUnitOfWork(Protocol):
    data: Any
    connection: Any

    def __enter__(self) -> 'DataPrivacyUnitOfWork': ...
    def __exit__(self, *exc: object) -> None: ...
    def commit(self) -> None: ...


DataPrivacyUnitOfWorkFactory = Callable[[], DataPrivacyUnitOfWork]
