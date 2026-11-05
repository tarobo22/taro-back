from datetime import date
from uuid import UUID

import pytest

from api.application.journal import JournalNotFound, JournalUseCases
from api.domain.journal import JournalEntry, JournalPolicy


OWNER, OTHER, ENTRY = UUID(int=2), UUID(int=3), UUID(int=1)


class OwnedRepository:
    def __init__(self):
        self.entry = JournalEntry(ENTRY, OWNER, date(2026, 11, 5), 'calm', 'before')
        self.updated = None

    def get(self, owner_id, entry_id):
        return self.entry if (owner_id, entry_id) == (OWNER, ENTRY) else None

    def update(self, entry):
        self.updated = entry
        return entry

    def list(self, owner_id):
        return [self.entry] if owner_id == OWNER else []

    def delete(self, owner_id, entry_id):
        self.deleted = (owner_id, entry_id)
        return (owner_id, entry_id) == (OWNER, ENTRY)


class EditUnitOfWork:
    def __init__(self):
        self.journals = OwnedRepository()
        self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def commit(self):
        self.commits += 1


def service(uow):
    return JournalUseCases(lambda: uow, JournalPolicy(frozenset({'calm', 'curious'}), 20),
                           lambda: pytest.fail('An edit must preserve the existing entry ID'))


def test_edit_preserves_identity_and_day_and_commits_new_body():
    uow = EditUnitOfWork()
    entry = service(uow).update(OWNER, ENTRY, 'curious', '  after  ')
    assert (entry.id, entry.owner_id, entry.day) == (ENTRY, OWNER, date(2026, 11, 5))
    assert (entry.mood, entry.body) == ('curious', 'after')
    assert uow.journals.updated == entry
    assert uow.commits == 1


def test_edit_does_not_write_when_owned_record_is_missing():
    uow = EditUnitOfWork()
    with pytest.raises(JournalNotFound):
        service(uow).update(OTHER, ENTRY, 'curious', 'after')
    assert uow.journals.updated is None
    assert uow.commits == 0


def test_list_requests_only_current_owners_records():
    uow = EditUnitOfWork()
    assert [x.id for x in service(uow).list(OWNER)] == [ENTRY]
    assert service(uow).list(OTHER) == []


def test_delete_uses_owner_and_entry_and_commits_once():
    uow = EditUnitOfWork()
    assert service(uow).delete(OWNER, ENTRY) is True
    assert uow.journals.deleted == (OWNER, ENTRY)
    assert uow.commits == 1
