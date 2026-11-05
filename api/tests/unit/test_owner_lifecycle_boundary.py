from types import SimpleNamespace
from uuid import UUID

import pytest

from api.adapters.experience_postgres import PostgresExperienceUnitOfWork
from api.adapters.owner_lifecycle import guard_owner as guard_transaction_owner
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.journal import InactiveOwnerError, guard_owner
from api.domain.identity import InactiveOwnerError as DomainInactiveOwnerError


OWNER = UUID(int=1)


class RecordingConnection:
    def __init__(self, events):
        self.events = events

    def execute(self, sql, parameters):
        assert sql == 'SELECT pg_advisory_xact_lock(hashtextextended(%s,0))'
        assert parameters == (f'yeoun:owner:{OWNER}',)
        self.events.append('owner-lock')


def test_application_delegates_to_the_unit_of_work_port_without_a_connection():
    calls = []
    callback = lambda transaction, owner: True
    unit = SimpleNamespace(guard_owner=lambda owner, guard: calls.append((owner, guard)))
    guard_owner(unit, OWNER, callback)
    assert calls == [(OWNER, callback)]


def test_missing_callback_performs_no_work_and_supports_existing_fake_units():
    guard_owner(object(), OWNER, None)


@pytest.mark.parametrize('active', [True, None])
def test_shared_adapter_locks_then_checks_the_same_transaction(active):
    events = []
    connection = RecordingConnection(events)
    def check(transaction, owner):
        assert transaction is connection and owner == OWNER
        events.append('active-check')
        return active
    guard_transaction_owner(connection, OWNER, check)
    assert events == ['owner-lock', 'active-check']


def test_inactive_exception_remains_import_compatible_and_is_raised_after_the_lock():
    assert InactiveOwnerError is DomainInactiveOwnerError
    events = []
    connection = RecordingConnection(events)
    def check(transaction, owner):
        events.append('inactive-check')
        return False
    with pytest.raises(InactiveOwnerError, match='owner_unavailable'):
        guard_transaction_owner(connection, OWNER, check)
    assert events == ['owner-lock', 'inactive-check']


def test_callback_exception_propagates_unchanged_without_further_adapter_work():
    events = []
    connection = RecordingConnection(events)
    failure = LookupError('fixture-only rejection')
    def reject(transaction, owner):
        events.append('callback-rejected')
        raise failure
    with pytest.raises(LookupError) as observed:
        guard_transaction_owner(connection, OWNER, reject)
    assert observed.value is failure and events == ['owner-lock', 'callback-rejected']


@pytest.mark.parametrize('unit_type', [PostgresJournalUnitOfWork, PostgresExperienceUnitOfWork])
def test_both_postgres_units_supply_the_guard_port_with_none_as_a_noop(unit_type):
    unit = unit_type(object())
    unit.guard_owner(OWNER, None)  # No connection has been acquired.
    events = []
    connection = RecordingConnection(events)
    unit.connection = connection
    def check(transaction, owner):
        assert transaction is connection and owner == OWNER
        events.append('active-check')
        return True
    unit.guard_owner(OWNER, check)
    assert events == ['owner-lock', 'active-check']
