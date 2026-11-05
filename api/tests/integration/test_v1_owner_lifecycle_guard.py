import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

from api.adapters.data_privacy_postgres import PostgresDataPrivacyUnitOfWork, owner_guard
from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
from api.adapters.experience_postgres import PostgresExperienceUnitOfWork
from api.adapters.local_guidance import LocalGuidance
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.data_privacy import DataPrivacyUseCases
from api.application.ai_jobs import AIJobsService
from api.application.experience import ExperienceUseCases
from api.application.journal import InactiveOwnerError, JournalUseCases
from api.domain.data_privacy import DataPolicy
from api.domain.experience import ExperiencePolicy
from api.domain.journal import JournalPolicy

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]
DAY = date(2026, 11, 6)


@pytest.fixture
def v1_lifecycle(pg_pool, owners):
    with pg_pool.connection() as c:
        for migration in sorted((ROOT / 'api/migrations').glob('*.sql')):
            c.execute(migration.read_text(encoding='utf-8'))
    rules = json.loads((ROOT / 'shared/experience-policy.json').read_text(encoding='utf-8'))
    content = json.loads((ROOT / 'shared/content.json').read_text(encoding='utf-8'))
    journal_policy = json.loads((ROOT / 'shared/journal-policy.json').read_text(encoding='utf-8'))
    ep = ExperiencePolicy(tuple(content['topics']), tuple(content['cards']), rules['spreads'],
                          tuple(content['talkQuestions']), rules['maxTarotQuestion'], rules['maxCounselQuestion'])
    jp = JournalPolicy(frozenset(content['moods']), journal_policy['maxBodyLength'])
    journal = JournalUseCases(lambda: PostgresJournalUnitOfWork(pg_pool), jp, uuid4, owner_guard=owner_guard)
    experience = ExperienceUseCases(lambda: PostgresExperienceUnitOfWork(pg_pool), ep,
                                    LocalGuidance(), owner_guard=owner_guard)
    data = DataPrivacyUseCases(lambda: PostgresDataPrivacyUnitOfWork(pg_pool),
        DataPolicy.from_mapping(json.loads((ROOT / 'shared/data-policy.json').read_text(encoding='utf-8'))),
        json.loads((ROOT / 'shared/data-catalog.json').read_text(encoding='utf-8')))
    owner = owners[0]
    me = data.get_me(owner)
    mood, topic = content['moods'][0], content['topics'][0]
    entry = journal.create(owner, DAY, mood, 'before erasure', uuid4())
    yield journal, experience, data, owner, entry, mood, topic, pg_pool, ep
    with pg_pool.connection() as c:
        for table in ('experience_operations', 'journal_create_requests', 'counsel_sessions', 'tarot_readings', 'daily_readings'):
            c.execute(f'DELETE FROM {table} WHERE session_id=ANY(%s)', (list(owners),))


def erased(context):
    _, _, data, owner, *_ = context
    request = data.create_data_request(owner, 'erase', '내 기록 모두 삭제', uuid4())
    assert data.process_request(UUID(request['id'])) is True


@pytest.mark.parametrize('operation', ['journal_create', 'reading', 'daily', 'counsel', 'ai_counsel'])
def test_authenticated_earlier_v1_writes_cannot_recreate_data_after_full_erase(v1_lifecycle, operation):
    journal, experience, _, owner, _, mood, topic, pool, _ = v1_lifecycle
    # The owner UUID represents a request authenticated before deletion completed.
    erased(v1_lifecycle)
    operations = {
        'journal_create': lambda: journal.create(owner, DAY, mood, 'late private text', uuid4()),
        'reading': lambda: experience.reading(owner, topic, 'late question', [0], 'one', uuid4()),
        'daily': lambda: experience.daily(owner, DAY, topic),
        'counsel': lambda: experience.counsel(owner, topic, 'late question', [], 'warm', uuid4()),
        'ai_counsel': lambda: experience.counsel(owner, topic, 'late question', [], 'warm', uuid4(), True, True),
    }
    with pytest.raises(InactiveOwnerError):
        operations[operation]()
    with pool.connection() as c:
        for table in ('journal_entries', 'journal_create_requests', 'tarot_readings', 'daily_readings', 'counsel_sessions', 'experience_operations'):
            assert c.execute(f'SELECT count(*) AS n FROM {table} WHERE session_id=%s', (owner,)).fetchone()['n'] == 0


@pytest.mark.parametrize('operation', ['journal_list', 'journal_get', 'experience_list'])
def test_reads_recheck_lifecycle_inside_their_uow_after_stale_auth(v1_lifecycle, operation):
    journal, experience, _, owner, entry, *_ = v1_lifecycle
    erased(v1_lifecycle)
    operations = {'journal_list': lambda: journal.list(owner),
                  'journal_get': lambda: journal.get(owner, entry.id),
                  'experience_list': lambda: experience.list(owner, 'reading')}
    with pytest.raises(InactiveOwnerError):
        operations[operation]()


@pytest.mark.parametrize('operation', ['update', 'delete'])
def test_v1_journal_update_delete_stop_when_erase_is_accepted(v1_lifecycle, operation):
    journal, _, data, owner, entry, mood, _, pool, _ = v1_lifecycle
    data.create_data_request(owner, 'erase', '내 기록 모두 삭제', uuid4())
    with pytest.raises(InactiveOwnerError):
        if operation == 'update':
            journal.update(owner, entry.id, mood, 'changed during erasure', entry.version)
        else:
            journal.delete(owner, entry.id)
    with pool.connection() as c:
        row = c.execute('SELECT body,version FROM journal_entries WHERE id=%s', (entry.id,)).fetchone()
        assert row == {'body': 'before erasure', 'version': entry.version}


def test_shared_owner_lock_serializes_local_counsel_before_erasure(v1_lifecycle):
    _, _, data, owner, _, _, topic, pool, ep = v1_lifecycle
    entered, release, attempting = Event(), Event(), Event()
    class BlockingLocal:
        def advise(self, *args):
            entered.set()
            assert release.wait(10)
            return {'empathy': 'heard', 'advice': 'reflect', 'action': 'rest', 'expression': 'listen'}
    experience = ExperienceUseCases(lambda: PostgresExperienceUnitOfWork(pool), ep,
                                    BlockingLocal(), owner_guard=owner_guard)
    def erase_request():
        attempting.set()
        return data.create_data_request(owner, 'erase', '내 기록 모두 삭제', uuid4())
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(experience.counsel, owner, topic, 'private', [], 'warm', uuid4())
        try:
            assert entered.wait(3)
            erasure = executor.submit(erase_request)
            assert attempting.wait(3)
            with pool.connection() as c:
                locked = c.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired', (f'yeoun:owner:{owner}',)).fetchone()['acquired']
                assert locked is False
            assert not erasure.done()
        finally:
            release.set()
        assert writer.result(timeout=5)['source'] == 'local'
        request = erasure.result(timeout=5)
    assert data.process_request(UUID(request['id'])) is True
    with pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM counsel_sessions WHERE session_id=%s', (owner,)).fetchone()['n'] == 0


def test_owner_lock_is_acquired_before_active_callback_for_late_journal_writer(v1_lifecycle):
    _, _, data, owner, _, mood, _, pool, _ = v1_lifecycle
    entered, release, writer_entered, guard_called = Event(), Event(), Event(), Event()
    def pause_erasure(connection, identity):
        entered.set()
        assert release.wait(10)
    data.erase_callbacks = (pause_erasure,)
    request = data.create_data_request(owner, 'erase', '내 기록 모두 삭제', uuid4())
    class TrackedUow:
        def __init__(self):
            self.inner = PostgresJournalUnitOfWork(pool)
        def __enter__(self):
            result = self.inner.__enter__()
            writer_entered.set()
            return result
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
    def tracked_guard(connection, identity):
        guard_called.set()
        return owner_guard(connection, identity)
    journal = JournalUseCases(TrackedUow, JournalPolicy(frozenset([mood]), 4000), uuid4,
                              owner_guard=tracked_guard)
    with ThreadPoolExecutor(max_workers=2) as executor:
        erasure = executor.submit(data.process_request, UUID(request['id']))
        try:
            assert entered.wait(3)
            late = executor.submit(journal.create, owner, DAY, mood, 'must not survive', uuid4())
            assert writer_entered.wait(3)
            assert not guard_called.is_set() and not late.done()
        finally:
            release.set()
        assert erasure.result(timeout=5)
        with pytest.raises(InactiveOwnerError):
            late.result(timeout=5)
    assert guard_called.is_set()


def test_general_owner_guard_and_consent_guard_release_transaction_for_provider_io(v1_lifecycle):
    _, _, _, owner, _, _, topic, pool, ep = v1_lifecycle
    checked = []
    class Provider:
        def advise(self, *args):
            with pool.connection() as c:
                acquired = c.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired',
                                     (f'yeoun:owner:{owner}',)).fetchone()['acquired']
                checked.append(acquired)
                assert acquired is True, 'owner lock leaked across external provider call'
            return {'empathy': 'heard', 'advice': 'reflect', 'action': 'rest', 'expression': 'listen'}
    ai = AIJobsService(lambda: PostgresAIJobsUnitOfWork(pool), owner_guard=owner_guard)
    ai.record_consent(owner, uuid4(), 'ai_processing', '2026-11-05', True)
    service = ExperienceUseCases(lambda: PostgresExperienceUnitOfWork(pool), ep, LocalGuidance(),
                                Provider(), ai_guard=ai.legacy_guard(), owner_guard=owner_guard)
    assert service.counsel(owner, topic, 'private', [], 'warm', uuid4(), True, True)['source'] == 'openai'
    assert checked == [True]
