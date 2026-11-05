from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest

from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.journal import JournalNotFound, JournalUseCases
from api.domain.journal import JournalEntry, JournalPolicy

pytestmark = pytest.mark.integration
POLICY = JournalPolicy(frozenset({'calm', 'curious'}), 40)


def service(pool):
    return JournalUseCases(lambda: PostgresJournalUnitOfWork(pool), POLICY, uuid4)


def test_postgres_roundtrip_and_edit_preserve_identity(pg_pool, owners):
    use_cases = service(pg_pool)
    saved = use_cases.create(owners[0], date(2026, 11, 6), 'calm', '  hello  ')
    assert saved.body == 'hello' and saved.created_at is not None
    edited = use_cases.update(owners[0], saved.id, 'curious', 'after')
    assert (edited.id, edited.day, edited.created_at) == (saved.id, saved.day, saved.created_at)
    assert use_cases.list(owners[0]) == [edited]


def test_leaving_without_explicit_commit_rolls_back(pg_pool, owners):
    entry = JournalEntry.create(entry_id=uuid4(), owner_id=owners[0], day=date(2026, 11, 6),
                                mood='calm', body='must not persist', policy=POLICY)
    with PostgresJournalUnitOfWork(pg_pool) as uow:
        uow.journals.add(entry)
    assert service(pg_pool).list(owners[0]) == []


def test_other_owner_cannot_read_update_or_delete(pg_pool, owners):
    use_cases = service(pg_pool)
    saved = use_cases.create(owners[0], date(2026, 11, 6), 'calm', 'private')
    assert use_cases.list(owners[1]) == []
    with PostgresJournalUnitOfWork(pg_pool) as uow:
        assert uow.journals.get(owners[1], saved.id) is None
        with pytest.raises(JournalNotFound):
            uow.journals.update(replace(saved, owner_id=owners[1], body='changed'))
    with pytest.raises(JournalNotFound):
        use_cases.update(owners[1], saved.id, 'curious', 'changed')
    assert use_cases.delete(owners[1], saved.id) is False
    assert use_cases.list(owners[0])[0].body == 'private'
    assert use_cases.delete(owners[0], saved.id) is True
    assert use_cases.list(owners[0]) == []
