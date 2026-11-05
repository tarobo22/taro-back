from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.ai_jobs_postgres import PostgresAIJobsUnitOfWork
from api.adapters.http.ai_jobs import create_ai_jobs_router
from api.adapters.http.journal_insights import create_journal_insights_router
from api.adapters.journal_insights_postgres import PostgresJournalInsightsUnitOfWork
from api.application.ai_jobs import AIJobsService
from api.application.journal_insights import JournalInsightsService
from api.domain.ai_jobs import AIError


pytestmark = pytest.mark.integration
RESULT = {'kind': 'insight', 'empathy': '마음을 기록해 주셨네요.', 'summary': '선택한 기록을 살펴봐요.',
          'card_insights': [], 'advice': '작은 선택을 해 봐요.', 'actions': ['쉬는 시간을 정해 보세요.'],
          'expression': 'listen', 'follow_up_questions': [], 'safety_mode': 'standard'}


class Provider:
    model = 'fake-insight-model'

    def __init__(self):
        self.calls, self.block, self.failure = [], False, None
        self.entered, self.release = Event(), Event()

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
def insight_env(pg_pool, owners):
    with pg_pool.connection() as tx:
        for migration in sorted((Path(__file__).parents[2] / 'migrations').glob('*.sql')):
            tx.execute(migration.read_text(encoding='utf-8'))
    provider = Provider()
    jobs = AIJobsService(lambda: PostgresAIJobsUnitOfWork(pg_pool), provider=provider)
    insights = JournalInsightsService(lambda: PostgresJournalInsightsUnitOfWork(pg_pool), ai_jobs=jobs)
    jobs.insight_source = insights
    owner = owners[0]
    consent_id = jobs.record_consent(owner, uuid4(), 'ai_processing', '2026-11-05', True)['id']
    jobs.record_consent(owner, uuid4(), 'journal_context', '2026-11-05', True)
    yield insights, jobs, provider, pg_pool, owners, consent_id


def journal(pool, owner, day='2026-11-05', body='selected private source', version=1):
    entry = uuid4()
    with pool.connection() as tx:
        tx.execute('INSERT INTO journal_entries(id,session_id,day,mood,body,version) VALUES(%s,%s,%s,%s,%s,%s)',
                   (entry, owner, day, 'calm', body, version))
    return str(entry)


def request(entries, consent_id, **changes):
    return {'range_start': '2026-11-01', 'range_end': '2026-11-06',
            'entry_ids': entries, 'consent_event_id': consent_id, **changes}


@contextmanager
def client_for(insights, jobs, owner):
    app = FastAPI()
    app.include_router(create_journal_insights_router(insights, lambda: owner))
    app.include_router(create_ai_jobs_router(jobs, lambda: owner))
    with TestClient(app) as client:
        yield client


def accepted(insight_env, **changes):
    insights, jobs, provider, pool, owners, consent_id = insight_env
    entry = journal(pool, owners[0])
    result = insights.create(owners[0], uuid4(), request([entry], consent_id, **changes))
    return result, entry


def test_http_ten_sources_are_private_idempotent_and_worker_result_is_an_insight(insight_env):
    insights, jobs, provider, pool, (owner, other), consent_id = insight_env
    entries = [journal(pool, owner, day='2026-11-01' if i == 0 else '2026-11-06', body=f'explicit-{i}') for i in range(10)]
    journal(pool, owner, body='unselected text must never be sent')
    key = str(uuid4())
    body = request(entries, consent_id, entry_versions={entry: 1 for entry in entries})
    with client_for(insights, jobs, owner) as client:
        response = client.post('/v2/journal-insights', json=body, headers={'X-Idempotency-Key': key})
        assert response.status_code == 202
        assert client.post('/v2/journal-insights', json=body, headers={'X-Idempotency-Key': key}).json() == response.json()
        job = response.json()
        queued = client.get('/v2/ai/jobs/' + job['job_id'])
        assert queued.status_code == 200 and queued.json()['kind'] == 'insight'
        assert client.get('/v2/journal-insights/' + job['resource_id']).json()['state'] == 'generating'
    assert not provider.calls
    with pytest.raises(AIError, match='NOT_FOUND'):
        insights.get(other, UUID(job['resource_id']))
    with pool.connection() as tx:
        payload = tx.execute('SELECT payload FROM p0_ai_jobs WHERE id=%s', (job['job_id'],)).fetchone()['payload']
        assert payload['context_versions'] == {entry: 1 for entry in entries}
        assert 'explicit-' not in json.dumps(payload)
        assert tx.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 1
    assert jobs.run_next('insight-worker')
    result = insights.get(owner, UUID(job['resource_id']))
    assert result == {'id': job['resource_id'], 'range_start': '2026-11-01', 'range_end': '2026-11-06',
                      'state': 'ready', 'based_on_count': 10, 'result': RESULT,
                      'entry_ids': entries, 'entry_versions': {entry: 1 for entry in entries}}
    sent = provider.calls[0]
    assert sent['kind'] == 'insight' and sent['cards'] == []
    assert {source['id'] for source in sent['journals']} == set(entries)
    assert 'unselected text' not in json.dumps(sent)
    assert jobs.get_job(owner, UUID(job['job_id']))['result_url'] == '/v2/journal-insights/' + job['resource_id']


@pytest.mark.parametrize('case,status', [('empty', 422), ('eleven', 422), ('duplicate', 422),
    ('foreign', 404), ('range', 422), ('reverse', 422), ('mismatch', 409),
    ('missing_map', 422), ('extra_map', 422), ('null_map', 422), ('bool_map', 422),
    ('duplicate_map', 422), ('numeric_date', 422), ('timestamp_date', 422)])
def test_rejected_http_admission_does_not_create_job_or_reserve_usage(insight_env, case, status):
    insights, jobs, provider, pool, (owner, other), consent_id = insight_env
    entry = journal(pool, other if case == 'foreign' else owner, day='2026-10-31' if case == 'range' else '2026-11-05', version=2)
    body = request([entry], consent_id)
    if case == 'empty': body['entry_ids'] = []
    if case == 'eleven': body['entry_ids'] = [str(uuid4()) for _ in range(11)]
    if case == 'duplicate': body['entry_ids'] = [entry, entry]
    if case == 'reverse': body['range_start'] = '2026-11-07'
    if case.endswith('map') or case == 'mismatch':
        body['entry_versions'] = {'mismatch': {entry: 1}, 'missing_map': {},
            'extra_map': {entry: 2, str(uuid4()): 1}, 'null_map': None, 'bool_map': {entry: True},
            'duplicate_map': {entry: 2, '{' + entry + '}': 2}}[case]
    if case == 'numeric_date': body['range_start'] = 0
    if case == 'timestamp_date': body['range_start'] = '2026-11-01T00:00:00'
    if case == 'mismatch':
        jobs.provider, jobs.policy['owner_daily_limit'] = None, 0
    with client_for(insights, jobs, owner) as client:
        response = client.post('/v2/journal-insights', json=body, headers={'X-Idempotency-Key': str(uuid4())})
    assert response.status_code == status, response.text
    assert not provider.calls
    with pool.connection() as tx:
        for table in ('p0_ai_jobs', 'p0_ai_usage_attempts', 'journal_insights'):
            assert tx.execute(f'SELECT count(*) AS n FROM {table} WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0


@pytest.mark.parametrize('kind', ['ai_processing', 'journal_context'])
def test_current_purpose_specific_consents_required_before_admission(insight_env, kind):
    insights, jobs, provider, pool, (owner, _), consent_id = insight_env
    entry = journal(pool, owner)
    jobs.record_consent(owner, uuid4(), kind, '2026-11-05', False)
    with pytest.raises(AIError, match='CONSENT_REQUIRED'):
        insights.create(owner, uuid4(), request([entry], consent_id))
    assert not provider.calls


@pytest.mark.parametrize('change', ['edit', 'delete', 'ai_processing', 'journal_context'])
@pytest.mark.parametrize('phase', ['queued', 'running', 'ready'])
def test_changed_sources_or_consent_invalidate_results_at_every_stage(insight_env, change, phase):
    insights, jobs, provider, pool, (owner, _), _ = insight_env
    job, entry = accepted(insight_env)
    provider.block = phase == 'running'
    def mutate():
        if change in ('ai_processing', 'journal_context'):
            jobs.record_consent(owner, uuid4(), change, '2026-11-05', False)
            jobs.record_consent(owner, uuid4(), change, '2026-11-05', True)
        else:
            with pool.connection() as tx:
                if change == 'edit': tx.execute('UPDATE journal_entries SET body=%s,version=version+1 WHERE id=%s', ('changed private source', entry))
                else: tx.execute('DELETE FROM journal_entries WHERE id=%s', (entry,))
    if phase == 'running':
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(jobs.run_next, 'slow-insight-worker')
            try:
                assert provider.entered.wait(3)
                mutate()
            finally:
                provider.release.set()
            assert future.result(timeout=5)
    elif phase == 'ready':
        assert jobs.run_next('insight-worker')
        mutate()
    else:
        mutate()
        jobs.run_next('insight-worker')
    result = insights.get(owner, UUID(job['resource_id']))
    assert result['state'] == 'invalidated' and result['result'] is None
    with pool.connection() as tx:
        assert tx.execute('SELECT result FROM journal_insights WHERE id=%s', (job['resource_id'],)).fetchone()['result'] is None
    assert len(provider.calls) == (0 if phase == 'queued' else 1)


def test_cancel_retry_reserves_new_attempt_only_explicitly_and_preserves_source_preconditions(insight_env):
    insights, jobs, provider, pool, (owner, _), _ = insight_env
    job, entry = accepted(insight_env)
    jobs.cancel_job(owner, UUID(job['job_id']), uuid4())
    assert insights.get(owner, UUID(job['resource_id']))['state'] == 'cancelled'
    assert jobs.run_next('worker') is False and not provider.calls
    retry_key = uuid4()
    retry = jobs.retry_job(owner, UUID(job['job_id']), retry_key)
    assert retry['job_id'] != job['job_id'] and retry['resource_id'] == job['resource_id']
    assert jobs.retry_job(owner, UUID(job['job_id']), retry_key) == retry
    jobs.cancel_job(owner, UUID(retry['job_id']), uuid4())
    with pool.connection() as tx:
        assert tx.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 2
        tx.execute('UPDATE journal_entries SET version=version+1 WHERE id=%s', (entry,))
    with pytest.raises(AIError, match='RESOURCE_CHANGED|CONTEXT_CHANGED'):
        jobs.retry_job(owner, UUID(retry['job_id']), uuid4())
    assert not provider.calls


def test_unknown_outcome_recovery_does_not_replay_and_source_delete_prevents_retry(insight_env):
    insights, jobs, provider, pool, (owner, _), _ = insight_env
    job, entry = accepted(insight_env)
    with pool.connection() as tx:
        tx.execute("UPDATE p0_ai_jobs SET state='running',provider_started=true,lease_id=%s,lease_until=now()-interval '1 second' WHERE id=%s", (uuid4(), job['job_id']))
    assert jobs.recover() >= 1
    assert jobs.get_job(owner, UUID(job['job_id']))['error']['code'] == 'UNKNOWN_PROVIDER_OUTCOME'
    assert insights.get(owner, UUID(job['resource_id']))['state'] == 'failed'
    assert jobs.run_next() is False and not provider.calls
    with pool.connection() as tx: tx.execute('DELETE FROM journal_entries WHERE id=%s', (entry,))
    with pytest.raises(AIError, match='RESOURCE_CHANGED'):
        jobs.retry_job(owner, UUID(job['job_id']), uuid4())


def test_owner_erasure_during_provider_io_cannot_recreate_insight_or_usage_identity(insight_env):
    insights, jobs, provider, pool, (owner, _), _ = insight_env
    job, _ = accepted(insight_env)
    provider.block = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(jobs.run_next, 'erasure-worker')
        try:
            assert provider.entered.wait(3)
            with pool.connection() as tx:
                insights.erase_owner(tx, owner)
                jobs.erase_owner(tx, owner)
        finally:
            provider.release.set()
        assert future.result(timeout=5)
    with pytest.raises(AIError, match='NOT_FOUND'):
        insights.get(owner, UUID(job['resource_id']))
    with pool.connection() as tx:
        assert tx.execute('SELECT count(*) AS n FROM journal_insights WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
        assert tx.execute('SELECT owner_id FROM p0_ai_usage_attempts WHERE id=%s', (job['job_id'],)).fetchone()['owner_id'] is None


def test_terminal_sse_only_exposes_committed_job_metadata_and_export_uses_separate_dataset(insight_env):
    insights, jobs, provider, pool, (owner, _), _ = insight_env
    job, entry = accepted(insight_env)
    assert jobs.run_next()
    with client_for(insights, jobs, owner) as client:
        response = client.get('/v2/ai/jobs/' + job['job_id'] + '/events')
        assert response.status_code == 200 and 'event: completed' in response.text
        assert 'selected private source' not in response.text and RESULT['summary'] not in response.text
        assert '"kind": "insight"' in response.text
    with pool.connection() as tx:
        dataset = insights.export_owner(tx, owner)
        assert dataset['journal_insights'][0]['entry_ids'] == [entry]
        assert dataset['journal_insights'][0]['result'] == RESULT
        assert 'journal_insights' not in jobs.export_owner(tx, owner)
        tx.execute('UPDATE journal_entries SET version=version+1 WHERE id=%s', (entry,))
        invalid = insights.export_owner(tx, owner)['journal_insights'][0]
        assert invalid['state'] == 'invalidated' and invalid['result'] is None


def test_p0_context_stays_three_and_all_ai_kinds_share_the_owner_concurrency_limit(insight_env):
    insights, jobs, provider, pool, (owner, _), consent_id = insight_env
    entries = [journal(pool, owner) for _ in range(4)]
    conversation = jobs.create_conversation(owner, uuid4(), 'care', 'warm')
    turn_body = {'client_message_id': str(uuid4()), 'input_source': 'text', 'text': '정리해요.',
                 'answers': [], 'consent_event_id': consent_id, 'context_entry_ids': entries}
    with pytest.raises(AIError, match='INVALID_CONTEXT'):
        jobs.create_turn(owner, UUID(conversation['id']), uuid4(), turn_body)
    job = insights.create(owner, uuid4(), request(entries, consent_id))
    with pytest.raises(AIError, match='AI_CONCURRENT_LIMIT'):
        jobs.create_turn(owner, UUID(conversation['id']), uuid4(), {**turn_body, 'context_entry_ids': []})
    jobs.cancel_job(owner, UUID(job['job_id']), uuid4())
    chat = jobs.create_turn(owner, UUID(conversation['id']), uuid4(), {**turn_body, 'context_entry_ids': []})
    with pytest.raises(AIError, match='AI_CONCURRENT_LIMIT'):
        insights.create(owner, uuid4(), request(entries, consent_id))
    jobs.cancel_job(owner, UUID(chat['job_id']), uuid4())
    assert not provider.calls


@pytest.mark.parametrize('limit', ['owner', 'global'])
def test_insights_inherit_daily_reservation_limits_and_cancellation_does_not_refund(limit, insight_env):
    insights, jobs, provider, pool, (owner, _), consent_id = insight_env
    assert jobs.policy['owner_daily_limit'] == 20 and jobs.policy['global_daily_limit'] == 200
    first, entry = accepted(insight_env)
    jobs.cancel_job(owner, UUID(first['job_id']), uuid4())
    if limit == 'owner':
        jobs.policy['owner_daily_limit'] = 1
    else:
        with PostgresAIJobsUnitOfWork(pool) as uow:
            jobs.policy['global_daily_limit'] = uow.ai_jobs.usage(owner)['total']
    with pytest.raises(AIError, match='AI_DAILY_LIMIT'):
        insights.create(owner, uuid4(), request([entry], consent_id))
    with pool.connection() as tx:
        assert tx.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 1
        assert tx.execute('SELECT count(*) AS n FROM journal_insights WHERE owner_id=%s', (owner,)).fetchone()['n'] == 1
    assert not provider.calls


def test_superseded_ai_consent_id_is_rejected_and_unavailable_provider_leaves_no_resource(insight_env):
    insights, jobs, provider, pool, (owner, _), old_consent = insight_env
    entry = journal(pool, owner)
    new_consent = jobs.record_consent(owner, uuid4(), 'ai_processing', '2026-11-05', True)['id']
    with pytest.raises(AIError, match='CONSENT_REQUIRED'):
        insights.create(owner, uuid4(), request([entry], old_consent))
    jobs.provider = None
    with pytest.raises(AIError, match='AI_UNAVAILABLE'):
        insights.create(owner, uuid4(), request([entry], new_consent))
    with pool.connection() as tx:
        assert tx.execute('SELECT count(*) AS n FROM journal_insights WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
        assert tx.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
    assert not provider.calls


def test_provider_failure_is_failed_until_explicit_retry_and_journal_hook_invalidates_completed_insight(insight_env):
    insights, jobs, provider, pool, (owner, _), _ = insight_env
    job, entry = accepted(insight_env)
    provider.failure = 'AI_RATE_LIMITED'
    assert jobs.run_next()
    assert insights.get(owner, UUID(job['resource_id']))['state'] == 'failed'
    assert jobs.run_next() is False and len(provider.calls) == 1
    provider.failure = None
    retried = jobs.retry_job(owner, UUID(job['job_id']), uuid4())
    assert jobs.run_next() and len(provider.calls) == 2
    assert insights.get(owner, UUID(job['resource_id']))['state'] == 'ready'
    with pool.connection() as tx:
        jobs.cancel_for_journal(tx, owner, UUID(entry))
        tx.execute('DELETE FROM journal_entries WHERE id=%s', (entry,))
    result = insights.get(owner, UUID(retried['resource_id']))
    assert result['state'] == 'invalidated' and result['result'] is None
    assert result['entry_ids'] == [entry]


def test_insight_lease_heartbeat_and_competing_workers_generate_once(insight_env):
    insights, jobs, provider, pool, (owner, _), _ = insight_env
    job, _ = accepted(insight_env)
    provider.block = True
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(jobs.run_next, 'first-insight-worker')
        try:
            assert provider.entered.wait(3)
            with pool.connection() as tx:
                lease = tx.execute('SELECT lease_id FROM p0_ai_jobs WHERE id=%s', (job['job_id'],)).fetchone()['lease_id']
            assert jobs.heartbeat(UUID(job['job_id']), lease)
            assert not jobs.heartbeat(UUID(job['job_id']), uuid4())
            assert jobs.run_next('second-insight-worker') is False
        finally:
            provider.release.set()
        assert future.result(timeout=5)
    assert len(provider.calls) == 1 and insights.get(owner, UUID(job['resource_id']))['state'] == 'ready'
