from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest

from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
from api.application.ai_jobs import AIJobsService
from api.domain.ai_jobs import AIError

pytestmark = pytest.mark.integration

RESULT = {'kind': 'chat', 'empathy': '고마워요.', 'summary': '정리해 봐요.', 'card_insights': [],
          'advice': '선택해 보세요.', 'actions': ['쉬어 봐요.'], 'expression': 'listen',
          'follow_up_questions': [], 'safety_mode': 'standard'}


class Provider:
    model = 'fake-model'

    def __init__(self, block=False, failure=None):
        self.calls = []
        self.entered, self.release = Event(), Event()
        self.block, self.failure = block, failure

    def available(self):
        return True

    def generate(self, payload):
        self.calls.append(payload)
        self.entered.set()
        if self.block:
            assert self.release.wait(10)
        if self.failure:
            raise AIError(self.failure, retryable=True)
        return dict(RESULT)


@pytest.fixture
def ai_env(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    factory = lambda: PostgresAIJobsUnitOfWork(pg_pool)
    provider = Provider()
    service = AIJobsService(factory, provider=provider)
    yield service, provider, pg_pool, owners, factory


def consent(service, owner, kind='ai_processing', granted=True):
    return service.record_consent(owner, uuid4(), kind, '2026-11-05', granted)['id']


def turn(service, owner, conversation, consent_id, key=None, **changes):
    return service.create_turn(owner, UUID(conversation['id']), key or uuid4(), {
        'client_message_id': str(uuid4()), 'input_source': 'text', 'text': '생각을 정리하고 싶어요.',
        'answers': [], 'context_entry_ids': [], 'consent_event_id': str(consent_id), **changes})


def test_consent_events_are_purpose_specific_append_only_and_idempotent(ai_env):
    service, _, _, owners, _ = ai_env
    owner, other = owners
    key = uuid4()
    event = service.record_consent(owner, key, 'ai_processing', '2026-11-05', True)
    assert service.record_consent(owner, key, 'ai_processing', '2026-11-05', True) == event
    with pytest.raises(AIError, match='IDEMPOTENCY_CONFLICT'):
        service.record_consent(owner, key, 'ai_processing', '2026-11-05', False)
    consent(service, owner, granted=False)
    assert len(service.list_consents(owner)['items']) == 2
    assert service.list_consents(other)['items'] == []
    with pytest.raises(AIError, match='CONSENT_POLICY_OUTDATED'):
        service.record_consent(owner, uuid4(), 'cloud_transcription', 'old', True)


def test_owner_scoped_conversations_ordered_turns_and_no_calls_on_reads(ai_env):
    service, provider, pool, owners, _ = ai_env
    owner, other = owners
    c = service.create_conversation(owner, uuid4(), 'care', 'warm')
    cid = UUID(c['id'])
    with pytest.raises(AIError, match='NOT_FOUND'):
        service.get_conversation(other, cid)
    accepted = turn(service, owner, c, consent(service, owner))
    assert accepted['state'] == 'queued' and not provider.calls
    assert service.run_next('test-worker') is True
    job = service.get_job(owner, UUID(accepted['job_id']))
    assert job['state'] == 'succeeded' and job['result_url'].endswith(accepted['resource_id'])
    assert service.get_turn(owner, cid, UUID(accepted['resource_id']))['assistant_result'] == RESULT
    assert service.list_turns(owner, cid)['items'][0]['turn_index'] == 1
    assert len(provider.calls) == 1
    assert service.close_conversation(owner, cid, uuid4())['state'] == 'closed'
    with pytest.raises(AIError, match='CONVERSATION_CLOSED'):
        turn(service, owner, c, service.list_consents(owner)['items'][0]['id'])
    service.delete_conversation(owner, cid)
    with pytest.raises(AIError, match='NOT_FOUND'):
        service.get_job(owner, UUID(accepted['job_id']))


def test_pending_idempotency_and_client_message_duplicates_reserve_once(ai_env):
    service, _, pool, owners, _ = ai_env
    owner = owners[0]
    c = service.create_conversation(owner, uuid4(), 'care', 'warm')
    consent_id = consent(service, owner)
    key, message = uuid4(), str(uuid4())
    first = turn(service, owner, c, consent_id, key, client_message_id=message)
    assert turn(service, owner, c, consent_id, key, client_message_id=message) == first
    assert turn(service, owner, c, consent_id, client_message_id=message) == first
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 1
    with pytest.raises(AIError, match='IDEMPOTENCY_CONFLICT'):
        turn(service, owner, c, consent_id, key, client_message_id=message, text='changed')


def test_revocation_during_external_call_discards_late_result_without_holding_db(ai_env):
    _, _, pool, owners, factory = ai_env
    provider = Provider(block=True)
    service = AIJobsService(factory, provider=provider)
    owner = owners[0]
    c = service.create_conversation(owner, uuid4(), 'care', 'giga')
    accepted = turn(service, owner, c, consent(service, owner))
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.run_next, 'slow-worker')
        try:
            assert provider.entered.wait(3)
            consent(service, owner, granted=False)
        finally:
            provider.release.set()
        assert pending.result(timeout=5)
    assert service.get_job(owner, UUID(accepted['job_id']))['state'] == 'cancelled'
    assert service.get_turn(owner, UUID(c['id']), UUID(accepted['resource_id']))['assistant_result'] is None


def test_expired_started_lease_never_calls_provider_again_and_retry_is_explicit(ai_env):
    service, provider, pool, owners, _ = ai_env
    owner = owners[0]
    c = service.create_conversation(owner, uuid4(), 'care', 'warm')
    accepted = turn(service, owner, c, consent(service, owner))
    jid = UUID(accepted['job_id'])
    with pool.connection() as connection:
        connection.execute("UPDATE p0_ai_jobs SET state='running',provider_started=true,lease_id=%s,lease_until=now()-interval '1 second' WHERE id=%s", (uuid4(), jid))
    assert service.recover() >= 1
    job = service.get_job(owner, jid)
    assert job['error']['code'] == 'UNKNOWN_PROVIDER_OUTCOME' and not provider.calls
    assert service.run_next('worker') is False
    retried = service.retry_job(owner, jid, uuid4())
    assert retried['job_id'] != str(jid) and retried['resource_id'] == accepted['resource_id']
    assert service.run_next('worker')
    assert len(service.list_turns(owner, UUID(c['id']))['items']) == 1


def test_selected_journals_require_separate_consent_and_are_rechecked_after_call(ai_env):
    _, _, pool, owners, factory = ai_env
    owner, other = owners
    provider = Provider(block=True)
    service = AIJobsService(factory, provider=provider)
    journal, foreign = uuid4(), uuid4()
    with pool.connection() as connection:
        for jid, oid, body in [(journal, owner, 'selected private text'), (foreign, other, 'never send')]:
            connection.execute("INSERT INTO journal_entries(id,session_id,day,mood,body) VALUES(%s,%s,current_date,'calm',%s)", (jid, oid, body))
    c = service.create_conversation(owner, uuid4(), 'care', 'warm')
    cid = consent(service, owner)
    with pytest.raises(AIError, match='CONSENT_REQUIRED'):
        turn(service, owner, c, cid, context_entry_ids=[str(journal)])
    consent(service, owner, kind='journal_context')
    with pytest.raises(AIError, match='NOT_FOUND'):
        turn(service, owner, c, cid, context_entry_ids=[str(foreign)])
    accepted = turn(service, owner, c, cid, context_entry_ids=[str(journal)])
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(service.run_next, 'worker')
        try:
            assert provider.entered.wait(3)
            assert provider.calls[0]['journals'][0]['body'] == 'selected private text'
            with pool.connection() as connection:
                connection.execute('UPDATE journal_entries SET version=version+1 WHERE id=%s', (journal,))
        finally:
            provider.release.set()
        pending.result(timeout=5)
    assert service.get_job(owner, UUID(accepted['job_id']))['state'] == 'cancelled'


def test_unavailable_provider_and_daily_limits_do_not_lose_input(ai_env):
    _, _, pool, owners, factory = ai_env
    owner = owners[0]
    service = AIJobsService(factory)
    c = service.create_conversation(owner, uuid4(), 'care', 'warm')
    cid = consent(service, owner)
    with pytest.raises(AIError, match='AI_UNAVAILABLE'):
        turn(service, owner, c, cid)
    assert service.list_turns(owner, UUID(c['id']))['items'] == []
    service = AIJobsService(factory, provider=Provider(), policy={'owner_daily_limit': 1})
    a = turn(service, owner, c, cid)
    service.cancel_job(owner, UUID(a['job_id']), uuid4())
    with pytest.raises(AIError, match='AI_DAILY_LIMIT'):
        turn(service, owner, c, cid)


def test_basic_conversation_remains_available_without_ai_consent_or_provider(ai_env):
    _, _, pool, owners, factory = ai_env
    service = AIJobsService(factory)
    owner = owners[0]
    c = service.create_conversation(owner, uuid4(), 'care', 'warm')
    a = turn(service, owner, c, None, use_ai=False, consent_event_id=None)
    assert service.run_next('local-worker')
    assert service.get_job(owner, UUID(a['job_id']))['state'] == 'succeeded'
    assert service.get_turn(owner, UUID(c['id']), UUID(a['resource_id']))['assistant_result']['kind'] == 'chat'
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
