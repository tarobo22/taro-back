from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest

from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
from api.application.ai_jobs import AIJobsService
from api.domain.ai_jobs import AIError
from api.tests.integration.test_p0_ai_jobs import Provider, consent
from api.tests.integration.test_reading_v2 import create, reading_env

pytestmark = pytest.mark.integration


class TarotProvider(Provider):
    def __init__(self, *args, bad=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.bad = bad

    def generate(self, payload):
        result = super().generate(payload)
        result['kind'] = 'tarot'
        result['card_insights'] = [{'card_id': c['card_id'], 'position': c['position'],
                                   'meaning': '뜻', 'reflection': '생각'} for c in payload['cards']]
        if self.bad:
            result['card_insights'][0]['card_id'] = (result['card_insights'][0]['card_id'] + 1) % 78
        return result


def connected(reading_env, provider=None):
    readings, _, _, owners, pool, data, deck, _ = reading_env
    provider = provider or TarotProvider()
    jobs = AIJobsService(lambda: PostgresAIJobsUnitOfWork(pool), provider=provider, reading_source=readings)
    readings.ai_jobs = jobs
    return readings, jobs, provider, owners, pool, data, deck


def interpret(readings, jobs, owner, deck, kind='daily', key=None):
    r = create(readings, owner, deck, kind)
    if kind == 'three_card':
        r = readings.select(owner, UUID(r['id']), uuid4(), r['version'], [s['id'] for s in r['slots'][6:9]])
    a = jobs.create_interpretation(owner, UUID(r['id']), key or uuid4(), {
        'expected_version': r['version'], 'consent_event_id': consent(jobs, owner), 'context_entry_ids': []})
    return r, a


def test_queued_cancel_marks_reading_failed_and_explicit_retry_preserves_cards(reading_env):
    readings, jobs, provider, owners, _, _, deck = connected(reading_env)
    owner = owners[0]
    r, a = interpret(readings, jobs, owner, deck, 'three_card')
    rid, jid = UUID(r['id']), UUID(a['job_id'])
    assert readings.get(owner, rid)['state'] == 'generating'
    jobs.cancel_job(owner, jid, uuid4())
    assert readings.get(owner, rid)['state'] == 'failed'
    assert not provider.calls
    retried = jobs.retry_job(owner, jid, uuid4())
    assert retried['resource_id'] == r['id'] and retried['job_id'] != a['job_id']
    assert jobs.run_next('worker')
    ready = readings.get(owner, rid)
    assert ready['state'] == 'ready' and ready['version'] == r['version'] + 1
    assert ready['selected_cards'] == r['selected_cards']
    assert provider.calls[0]['cards'] == r['selected_cards']


def test_card_mismatch_is_failed_not_partial_result_and_basic_stays_available(reading_env):
    readings, jobs, provider, owners, _, _, deck = connected(reading_env, TarotProvider(bad=True))
    owner = owners[0]
    r, a = interpret(readings, jobs, owner, deck)
    assert jobs.run_next('worker')
    job = jobs.get_job(owner, UUID(a['job_id']))
    failed = readings.get(owner, UUID(r['id']))
    assert job['state'] == 'failed' and job['error']['code'] == 'AI_OUTPUT_INVALID'
    assert job['result_url'] is None and failed['result'] is None and failed['state'] == 'failed'
    assert readings.basic(owner, UUID(r['id']))['source'] == 'basic'


def test_delete_reading_during_external_call_prevents_late_result_resurrection(reading_env):
    readings, jobs, provider, owners, pool, _, deck = connected(reading_env, TarotProvider(block=True))
    owner = owners[0]
    r, a = interpret(readings, jobs, owner, deck)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(jobs.run_next, 'slow-worker')
        try:
            assert provider.entered.wait(3)
            readings.delete(owner, UUID(r['id']))
        finally:
            provider.release.set()
        assert pending.result(timeout=5)
    assert jobs.get_job(owner, UUID(a['job_id']))['state'] == 'cancelled'
    with pytest.raises(AIError, match='NOT_FOUND'):
        readings.get(owner, UUID(r['id']))
    replacement = create(readings, owner, deck, 'daily')
    assert replacement['selected_cards'] == r['selected_cards'] and replacement['result'] is None


def test_real_privacy_erasure_during_provider_call_clears_reading_jobs_and_context(reading_env):
    readings, jobs, provider, owners, pool, data, deck = connected(reading_env, TarotProvider(block=True))
    owner = owners[0]
    r, a = interpret(readings, jobs, owner, deck)
    data.erase_callbacks = (jobs.erase_owner, readings.erase_owner)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(jobs.run_next, 'slow-worker')
        try:
            assert provider.entered.wait(3)
            erasure = data.create_data_request(owner, 'erase', '내 기록 모두 삭제', uuid4())
            assert data.process_request(UUID(erasure['id'])) is True
        finally:
            provider.release.set()
        assert pending.result(timeout=5)
    with pool.connection() as c:
        for table in ('p0_readings', 'p0_daily_draws', 'p0_reading_commands', 'p0_ai_jobs', 'ai_consent_events'):
            assert c.execute(f'SELECT count(*) AS n FROM {table} WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
        assert c.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE id=%s AND owner_id IS NULL', (UUID(a['job_id']),)).fetchone()['n'] == 1
    with pytest.raises(AIError, match='OWNER_UNAVAILABLE'):
        readings.get(owner, UUID(r['id']))


def test_skip_locked_worker_processes_another_owners_job_once(reading_env):
    readings, jobs, provider, owners, pool, _, deck = connected(reading_env)
    _, first = interpret(readings, jobs, owners[0], deck)
    _, second = interpret(readings, jobs, owners[1], deck)
    with pool.connection() as c:
        c.execute('SELECT id FROM p0_ai_jobs WHERE id=%s FOR UPDATE', (UUID(first['job_id']),)).fetchone()
        assert jobs.run_next('skip-locked-worker')
        assert jobs.get_job(owners[1], UUID(second['job_id']))['state'] == 'succeeded'
    assert jobs.run_next('worker')
    assert jobs.run_next('worker') is False and len(provider.calls) == 2
