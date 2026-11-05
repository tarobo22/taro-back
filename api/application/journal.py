from dataclasses import replace
from datetime import date
from typing import Callable
from uuid import UUID

from api.application.ports import JournalUnitOfWork
from api.domain.journal import JournalEntry, JournalPolicy, JournalVersionConflict


class JournalNotFound(LookupError):
    pass


class JournalUseCases:
    def __init__(self, uow_factory: Callable[[], JournalUnitOfWork],
                 policy: JournalPolicy, new_id: Callable[[], UUID]):
        self._uow = uow_factory
        self._policy = policy
        self._new_id = new_id

    def create(self, owner_id: UUID, day: date, mood: str, body: str,
               request_id: UUID | None = None) -> JournalEntry:
        entry = JournalEntry.create(entry_id=self._new_id(), owner_id=owner_id,
                                    day=day, mood=mood, body=body, policy=self._policy)
        with self._uow() as uow:
            saved = (uow.journals.add_once(entry, request_id) if request_id is not None
                     else uow.journals.add(entry))
            uow.commit()
            return saved

    def update(self, owner_id: UUID, entry_id: UUID, mood: str, body: str,
               expected_version: int | None = None) -> JournalEntry:
        with self._uow() as uow:
            current = uow.journals.get(owner_id, entry_id)
            if current is None:
                raise JournalNotFound(entry_id)
            if expected_version is not None and current.version != expected_version:
                raise JournalVersionConflict('stale_entry')
            validated = JournalEntry.create(entry_id=current.id, owner_id=current.owner_id,
                                            day=current.day, mood=mood, body=body,
                                            policy=self._policy)
            changed = replace(validated, created_at=current.created_at,
                              updated_at=current.updated_at, version=current.version)
            saved = uow.journals.update(changed)
            uow.commit()
            return saved

    def list(self, owner_id: UUID) -> list[JournalEntry]:
        with self._uow() as uow:
            return uow.journals.list(owner_id)

    def delete(self, owner_id: UUID, entry_id: UUID) -> bool:
        with self._uow() as uow:
            deleted = uow.journals.delete(owner_id, entry_id)
            uow.commit()
            return deleted
