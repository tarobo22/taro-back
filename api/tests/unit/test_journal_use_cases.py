from datetime import date
from uuid import UUID

import pytest

from api.application.journal import JournalUseCases
from api.domain.journal import JournalPolicy, JournalRuleViolation


OWNER = UUID(int=2)


class RecordingRepository:
    def __init__(self):
        self.saved = None
        self.failure = None

    def add(self, entry):
        if self.failure:
            raise self.failure
        self.saved = entry
        return entry


class RecordingUnitOfWork:
    def __init__(self):
        self.journals = RecordingRepository()
        self.commits = 0
        self.entered = False
        self.exit_error = None

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, kind, error, traceback):
        self.exit_error = error

    def commit(self):
        self.commits += 1


def service(uow):
    return JournalUseCases(lambda: uow,
                           JournalPolicy(frozenset({'calm'}), 20), lambda: UUID(int=1))


def test_successful_save_commits_exactly_once():
    uow = RecordingUnitOfWork()
    result = service(uow).create(OWNER, date(2026, 11, 5), 'calm', '  one line  ')
    assert result.body == 'one line'
    assert uow.journals.saved.owner_id == OWNER
    assert uow.commits == 1


def test_invalid_input_never_opens_a_transaction():
    uow = RecordingUnitOfWork()
    with pytest.raises(JournalRuleViolation):
        service(uow).create(OWNER, date(2026, 11, 5), 'unknown', 'one line')
    assert not uow.entered
    assert uow.commits == 0


def test_persistence_failure_never_commits_or_becomes_success():
    uow = RecordingUnitOfWork()
    error = RuntimeError('storage unavailable')
    uow.journals.failure = error
    with pytest.raises(RuntimeError, match='storage unavailable'):
        service(uow).create(OWNER, date(2026, 11, 5), 'calm', 'one line')
    assert uow.commits == 0
    assert uow.exit_error is error
