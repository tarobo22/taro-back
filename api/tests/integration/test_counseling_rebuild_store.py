from pathlib import Path
from uuid import uuid4

import pytest

from api.adapters.counseling_postgres import PostgresCounselingStore

pytestmark = pytest.mark.integration


@pytest.fixture
def store(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[3] / 'api/migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    return PostgresCounselingStore(pg_pool)


def test_glossary_rls_filters_even_queries_without_an_owner_predicate(store, owners):
    a, b = owners
    with store.transaction(a) as repo:
        repo.save_definition(a, {'term': '반짝', 'definition': 'A_ONLY'})
    with store.transaction(b) as repo:
        repo.save_definition(b, {'term': '반짝', 'definition': 'B_ONLY'})
        rows = repo.connection.execute('SELECT definition FROM personal_glossary').fetchall()
        assert rows == [{'definition': 'B_ONLY'}]
    with store.transaction() as repo:
        assert repo.connection.execute('SELECT count(*) AS n FROM personal_glossary').fetchone()['n'] == 0


def test_definition_correction_updates_one_normalized_entry_and_rolls_back_on_error(store, owners):
    owner = owners[0]
    with store.transaction(owner) as repo:
        first = repo.save_definition(owner, {'term': 'ＲＡＧ', 'definition': '첫 의미'})
    with store.transaction(owner) as repo:
        second = repo.save_definition(owner, {'term': 'rag', 'definition': '고친 의미'})
        assert second['id'] == first['id'] and second['version'] == 2
    with pytest.raises(RuntimeError, match='abort'):
        with store.transaction(owner) as repo:
            repo.delete_definition(owner, first['id'])
            raise RuntimeError('abort')
    with store.transaction(owner) as repo:
        assert [row['definition'] for row in repo.definitions(owner)] == ['고친 의미']


def queued(store, owner):
    session_id, command_id, consent_id = uuid4(), uuid4(), uuid4()
    with store.pool.connection() as connection:
        connection.execute('INSERT INTO ai_consent_events(id,owner_id,kind,policy_version,granted) VALUES(%s,%s,%s,%s,true)',
                           (consent_id, owner, 'ai_processing', '2026-11-05'))
    with store.transaction(owner) as repo:
        repo.create_session(owner, session_id, 'care', {'phase': 'listening', 'history': []}, uuid4())
        repo.enqueue(owner, session_id, uuid4(), 'fingerprint', 'message', {'text': 'PRIVATE_STORY'}, consent_id, command_id)
    return command_id, session_id


def test_dispatcher_returns_identifiers_only_and_never_claims_a_job_twice(store, owners):
    command_id, session_id = queued(store, owners[0])
    claimed = store.claim()
    assert claimed == {'id': command_id, 'owner_id': owners[0], 'session_id': session_id}
    assert store.claim() is None
    with store.transaction(owners[0]) as repo:
        assert repo.claimed(command_id)['state'] == 'running'
        repo.finish(command_id)
        assert repo.latest_command(owners[0], session_id)['state'] == 'succeeded'


def test_expired_model_work_is_failed_without_automatic_paid_replay(store, owners):
    command_id, _ = queued(store, owners[0])
    store.claim()
    with store.transaction(owners[0]) as repo:
        repo.connection.execute("UPDATE counseling_commands SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (command_id,))
    assert store.claim() is None
    with store.transaction(owners[0]) as repo:
        row = repo.connection.execute('SELECT state,error_code FROM counseling_commands WHERE id=%s', (command_id,)).fetchone()
        assert row == {'state': 'failed', 'error_code': 'UNKNOWN_PROVIDER_OUTCOME'}
