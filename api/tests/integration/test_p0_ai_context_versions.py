from contextlib import contextmanager
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.ai_jobs import create_ai_jobs_router
from api.domain.ai_jobs import AIError, fingerprint
from api.tests.integration.test_p0_ai_jobs import ai_env, consent, turn
from api.tests.integration.test_p0_reading_ai_races import connected
from api.tests.integration.test_reading_v2 import create, reading_env


pytestmark = pytest.mark.integration


def journal(pool, owner, version=1):
    entry_id = uuid4()
    with pool.connection() as connection:
        connection.execute('''INSERT INTO journal_entries(id,session_id,day,mood,body,version)
            VALUES(%s,%s,current_date,'calm','selected journal',%s)''', (entry_id, owner, version))
    return entry_id


@contextmanager
def client_for(service, owner):
    app = FastAPI()
    app.include_router(create_ai_jobs_router(service, lambda: owner))
    with TestClient(app) as client:
        yield client


def prepared(ai_env, version=1):
    service, provider, pool, owners, factory = ai_env
    owner = owners[0]
    entry_id = journal(pool, owner, version)
    conversation = service.create_conversation(owner, uuid4(), 'care', 'warm')
    consent_id = consent(service, owner)
    consent(service, owner, kind='journal_context')
    body = {'client_message_id': str(uuid4()), 'input_source': 'text', 'text': '생각을 정리해요.',
            'context_entry_ids': [str(entry_id)], 'consent_event_id': consent_id}
    return service, provider, pool, owner, entry_id, conversation, body


def test_supplied_context_versions_are_checked_and_reach_the_worker_without_extra_text_cache(ai_env):
    service, provider, pool, owner, entry_id, conversation, body = prepared(ai_env)
    body['context_entry_versions'] = {str(entry_id): 1}
    with client_for(service, owner) as client:
        response = client.post('/v2/conversations/' + conversation['id'] + '/turns', json=body,
                               headers={'X-Idempotency-Key': str(uuid4())})
    assert response.status_code == 202 and not provider.calls
    with pool.connection() as connection:
        payload = connection.execute('SELECT payload FROM p0_ai_jobs WHERE id=%s', (response.json()['job_id'],)).fetchone()['payload']
        assert payload['context_versions'] == {str(entry_id): 1}
        assert 'selected journal' not in str(payload)
    assert service.run_next('context-version-worker') is True
    assert provider.calls[0]['journals'][0]['version'] == 1


def test_changed_context_is_409_before_provider_availability_and_quota_reservation(ai_env):
    service, provider, pool, owner, entry_id, conversation, body = prepared(ai_env, version=2)
    body['context_entry_versions'] = {str(entry_id): 1}
    service.policy['owner_daily_limit'] = 0
    service.provider = None
    with client_for(service, owner) as client:
        response = client.post('/v2/conversations/' + conversation['id'] + '/turns', json=body,
                               headers={'X-Idempotency-Key': str(uuid4())})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'RESOURCE_CHANGED'
    assert not provider.calls
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
        assert connection.execute('SELECT count(*) AS n FROM p0_ai_jobs WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
        assert connection.execute('SELECT count(*) AS n FROM ai_conversation_turns WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0


@pytest.mark.parametrize('mapping', ['missing', 'extra', 'wrong', 'boolean', 'float', 'string', 'zero', 'null', 'invalid_uuid'])
def test_context_version_mapping_requires_exact_selected_uuid_keys_and_strict_positive_values(ai_env, mapping):
    service, provider, pool, owner, entry_id, conversation, body = prepared(ai_env)
    versions = {
        'missing': {}, 'extra': {str(entry_id): 1, str(uuid4()): 1}, 'wrong': {str(uuid4()): 1},
        'boolean': {str(entry_id): True}, 'float': {str(entry_id): 1.0}, 'string': {str(entry_id): '1'},
        'zero': {str(entry_id): 0}, 'null': None, 'invalid_uuid': {'not-a-uuid': 1},
    }[mapping]
    body['context_entry_versions'] = versions
    with client_for(service, owner) as client:
        response = client.post('/v2/conversations/' + conversation['id'] + '/turns', json=body,
                               headers={'X-Idempotency-Key': str(uuid4())})
    assert response.status_code == 422 and not provider.calls
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0


def test_context_versions_do_not_replace_ownership_checks(ai_env):
    service, provider, pool, owner, entry_id, conversation, body = prepared(ai_env)
    foreign = journal(pool, ai_env[3][1])
    body.update(context_entry_ids=[str(foreign)], context_entry_versions={str(foreign): 1})
    with client_for(service, owner) as client:
        response = client.post('/v2/conversations/' + conversation['id'] + '/turns', json=body,
                               headers={'X-Idempotency-Key': str(uuid4())})
    assert response.status_code == 404 and not provider.calls


def test_omitted_context_versions_preserve_legacy_admission_and_idempotency_payload(ai_env):
    service, provider, pool, owner, entry_id, conversation, body = prepared(ai_env, version=2)
    key = uuid4()
    with client_for(service, owner) as client:
        response = client.post('/v2/conversations/' + conversation['id'] + '/turns', json=body,
                               headers={'X-Idempotency-Key': str(key)})
        replay = client.post('/v2/conversations/' + conversation['id'] + '/turns', json=body,
                             headers={'X-Idempotency-Key': str(key)})
    assert response.status_code == 202 and replay.json() == response.json()
    with pool.connection() as connection:
        prior_shape = {'answers': [], 'use_ai': True, **body}
        operation = connection.execute('SELECT fingerprint FROM p0_ai_idempotency WHERE owner_id=%s AND key=%s',
                                       (owner, key)).fetchone()
        assert operation['fingerprint'] == fingerprint(prior_shape)
    jid = UUID(response.json()['job_id'])
    service.cancel_job(owner, jid, uuid4())
    with pool.connection() as connection:
        connection.execute('UPDATE journal_entries SET version=version+1 WHERE id=%s', (entry_id,))
    with pytest.raises(AIError, match='CONTEXT_CHANGED'):
        service.retry_job(owner, jid, uuid4())


def test_interpretation_rejects_changed_selected_journal_without_changing_reading_or_usage(reading_env):
    readings, jobs, provider, owners, pool, data, deck = connected(reading_env)
    owner = owners[0]
    reading = create(readings, owner, deck, kind='daily')
    entry_id = journal(pool, owner, version=2)
    ai_consent = consent(jobs, owner)
    consent(jobs, owner, kind='journal_context')
    body = {'expected_version': reading['version'], 'consent_event_id': ai_consent,
            'context_entry_ids': [str(entry_id)], 'context_entry_versions': {str(entry_id): 1}}
    with client_for(jobs, owner) as client:
        response = client.post('/v2/readings/' + reading['id'] + '/interpretations', json=body,
                               headers={'X-Idempotency-Key': str(uuid4())})
        assert response.status_code == 409 and response.json()['detail']['code'] == 'RESOURCE_CHANGED'
        assert readings.get(owner, UUID(reading['id']))['state'] == reading['state']
        with pool.connection() as connection:
            assert connection.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
        body['context_entry_versions'] = {str(entry_id): 2}
        accepted = client.post('/v2/readings/' + reading['id'] + '/interpretations', json=body,
                               headers={'X-Idempotency-Key': str(uuid4())})
    assert accepted.status_code == 202
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM p0_ai_usage_attempts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 1
    assert jobs.run_next('context-reading-worker') is True
    assert provider.calls[0]['journals'][0]['version'] == 2
