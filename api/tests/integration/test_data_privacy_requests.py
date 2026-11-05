import hashlib
import io
import json
from datetime import timedelta
from uuid import uuid4
from zipfile import ZipFile

import pytest
from psycopg.types.json import Jsonb

from api.domain.data_privacy import DataConflict, DataExpired, DataNotFound, InvalidDataInput
from api.tests.integration.test_data_privacy_profile_catalog import data_context


pytestmark = pytest.mark.integration


def seed_private_records(pool, owners):
    with pool.connection() as connection:
        for owner, text in zip(owners, ('my private journal', 'OTHER_OWNER_PRIVATE_TEXT')):
            entry = uuid4()
            connection.execute('''INSERT INTO journal_entries(id,session_id,day,mood,body)
                VALUES(%s,%s,'2026-11-06','calm',%s)''', (entry, owner, text))
            connection.execute('''INSERT INTO journal_create_requests(session_id,request_id,fingerprint,result)
                VALUES(%s,%s,'internal-secret-fingerprint',%s)''', (owner, uuid4(), Jsonb({'body': text})))
            connection.execute('''INSERT INTO tarot_readings(id,session_id,topic,question,card_ids,result)
                VALUES(%s,%s,'self',%s,ARRAY[0],%s)''', (uuid4(), owner, text, Jsonb({'summary': text})))
            connection.execute('''INSERT INTO daily_readings(session_id,day,topic,result)
                VALUES(%s,'2026-11-06','self',%s)''', (owner, Jsonb({'summary': text})))
            connection.execute('''INSERT INTO counsel_sessions(id,session_id,topic,question,answers,tone,advice)
                VALUES(%s,%s,'self',%s,'[]','warm',%s)''', (uuid4(), owner, text, Jsonb({'advice': text})))
            connection.execute('''INSERT INTO experience_operations(session_id,kind,request_id,fingerprint,result)
                VALUES(%s,'reading',%s,'internal-secret-fingerprint',%s)''', (owner, uuid4(), Jsonb({'question': text})))
            connection.execute('''INSERT INTO experience_ai_jobs(session_id,request_id,fingerprint,lease_id,lease_until,state)
                VALUES(%s,%s,'internal-secret-fingerprint',%s,now()+interval '1 hour','pending')''',
                (owner, uuid4(), uuid4()))
            connection.execute('INSERT INTO ai_usage_attempts(id,session_id,request_id) VALUES(%s,%s,%s)',
                               (uuid4(), owner, uuid4()))


def test_export_is_queued_then_builds_owned_json_csv_zip_without_internal_credentials(data_context):
    client, current, service, owners, pool = data_context
    seed_private_records(pool, owners)
    observed = []
    def export_context(connection, owner):
        observed.append(owner)
        assert connection.execute('SELECT 1 AS n').fetchone()['n'] == 1
        return {'conversations': [{'id': str(uuid4()), 'text': '=SUM(1,1)', 'owner_id': str(owner)}]}
    service.export_callbacks = (export_context,)
    accepted = service.create_data_request(owners[0], 'export', None, uuid4())
    assert accepted['state'] == 'queued'
    with pytest.raises(DataConflict):
        service.download_export(owners[0], accepted['id'])
    assert service.get_data_request(accepted['id'], owner=owners[0])['state'] == 'queued'
    assert service.process_request(accepted['id']) is True
    assert observed == [owners[0]]
    complete = service.get_data_request(accepted['id'], owner=owners[0])
    assert complete['state'] == 'completed' and complete['download_available'] is True
    archive = service.download_export(owners[0], accepted['id'])
    with ZipFile(io.BytesIO(archive)) as zipped:
        assert {'manifest.json', 'data.json', 'journal_entries.csv', 'conversations.csv'} <= set(zipped.namelist())
        data = json.loads(zipped.read('data.json'))
        assert data['profile']['id'] == str(owners[0])
        assert data['journal_entries'][0]['body'] == 'my private journal'
        assert len(data['readings']) == len(data['daily_readings']) == len(data['counsel_sessions']) == 1
        assert "'=SUM(1,1)" in zipped.read('conversations.csv').decode('utf-8-sig')
        combined = b'\n'.join(zipped.read(name) for name in zipped.namelist()).decode('utf-8-sig')
        for forbidden in ('token_hash', 'receipt_hash', 'idempotency_hash', 'internal-secret-fingerprint', 'OTHER_OWNER_PRIVATE_TEXT'):
            assert forbidden not in combined
    # Completed workers are idempotent and never recreate snapshots on a retry.
    assert service.process_request(accepted['id']) is False
    assert observed == [owners[0]]


def test_data_request_http_receipts_are_exact_and_never_authorize_download(data_context):
    client, current, service, owners, pool = data_context
    key = {'X-Idempotency-Key': str(uuid4())}
    first = client.post('/v2/me/data-requests', json={'kind': 'export'}, headers=key)
    assert first.status_code == 202 and first.json()['state'] == 'queued'
    request = first.json()
    replay = client.post('/v2/me/data-requests', json={'kind': 'export'}, headers=key)
    assert replay.status_code == 202
    assert replay.json()['id'] == request['id'] and replay.json()['receipt'] == request['receipt']
    assert client.post('/v2/me/data-requests', json={'kind': 'erase', 'confirmation': '내 기록 모두 삭제'}, headers=key).status_code == 409
    status_path = '/v2/data-requests/' + request['id']
    assert client.get(status_path).json()['download_available'] is True
    download = client.get(status_path + '/download')
    assert download.status_code == 200 and download.headers['content-type'] == 'application/zip'
    assert download.headers['cache-control'] == 'no-store'
    assert download.headers['content-disposition'].startswith('attachment;')
    current[0] = owners[1]
    assert client.get(status_path).status_code == client.get('/v2/data-requests/' + str(uuid4())).status_code == 404
    receipt_headers = {'X-Data-Receipt': request['receipt']}
    receipt_status = client.get(status_path, headers=receipt_headers)
    assert receipt_status.status_code == 200 and receipt_status.json()['download_available'] is False
    assert client.get(status_path + '/download', headers=receipt_headers).status_code == 404
    assert client.get(status_path, headers={'X-Data-Receipt': 'incorrect'}).status_code == 404
    second = client.post('/v2/me/data-requests', json={'kind': 'export'}, headers={'X-Idempotency-Key': str(uuid4())}).json()
    assert client.get('/v2/data-requests/' + second['id'], headers=receipt_headers).status_code == 404


def test_export_expiry_rejects_access_and_removes_private_bytes(data_context):
    client, current, service, owners, pool = data_context
    accepted = service.create_data_request(owners[0], 'export', None, uuid4())
    service.process_request(accepted['id'])
    later = service.clock() + timedelta(hours=service.policy.export_file_hours, seconds=1)
    service.clock = lambda: later
    with pytest.raises(DataExpired):
        service.download_export(owners[0], accepted['id'])
    with pool.connection() as connection:
        assert connection.execute('SELECT export_bytes FROM data_privacy_requests WHERE id=%s',
                                  (accepted['id'],)).fetchone()['export_bytes'] is None
    with pytest.raises(DataExpired):
        service.get_data_request(accepted['id'], receipt=accepted['receipt'])


def test_full_erasure_is_separate_from_acceptance_revokes_old_auth_and_detaches_receipt(data_context):
    client, current, service, owners, pool = data_context
    seed_private_records(pool, owners)
    service.update_preferences(owners[0], {'tone': 'giga', 'timezone': 'America/New_York'})
    client.post('/v2/feedback', json={'rating': 'helpful', 'reason': 'clear', 'comment': 'private feedback'},
                headers={'X-Idempotency-Key': str(uuid4())})
    export = service.create_data_request(owners[0], 'export', None, uuid4())
    service.process_request(export['id'])
    with pool.connection() as connection:
        original_hash = connection.execute('SELECT token_hash FROM app_sessions WHERE id=%s', (owners[0],)).fetchone()['token_hash']
    callbacks = []
    def erase_context(connection, owner):
        assert connection.execute('SELECT state FROM data_privacy_profiles WHERE owner_id=%s', (owner,)).fetchone()['state'] == 'deleting'
        callbacks.append(owner)
    service.erase_callbacks = (erase_context,)
    accepted = service.create_data_request(owners[0], 'erase', '내 기록 모두 삭제', uuid4())
    assert accepted['state'] == 'queued'
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM journal_entries WHERE session_id=%s', (owners[0],)).fetchone()['n'] == 1
    assert client.patch('/v2/me/preferences', json={'tone': 'warm'}).status_code == 409
    assert client.post('/v2/feedback', json={'rating': 'helpful', 'reason': 'clear'},
                       headers={'X-Idempotency-Key': str(uuid4())}).status_code == 409
    service.process_request(accepted['id'])
    assert callbacks == [owners[0]]
    with pool.connection() as connection:
        for table in ('journal_entries', 'journal_create_requests', 'tarot_readings', 'daily_readings',
                      'counsel_sessions', 'experience_operations', 'experience_ai_jobs'):
            assert connection.execute(f'SELECT count(*) AS n FROM {table} WHERE session_id=%s', (owners[0],)).fetchone()['n'] == 0
        assert connection.execute('SELECT count(*) AS n FROM data_privacy_feedback WHERE owner_id=%s', (owners[0],)).fetchone()['n'] == 0
        assert connection.execute('SELECT count(*) AS n FROM ai_usage_attempts WHERE session_id=%s', (owners[0],)).fetchone()['n'] == 1
        assert connection.execute('SELECT count(*) AS n FROM journal_entries WHERE session_id=%s', (owners[1],)).fetchone()['n'] == 1
        assert connection.execute('SELECT token_hash FROM app_sessions WHERE id=%s', (owners[0],)).fetchone()['token_hash'] != original_hash
        profile = connection.execute('SELECT state,timezone,tone FROM data_privacy_profiles WHERE owner_id=%s', (owners[0],)).fetchone()
        assert profile == {'state': 'erased', 'timezone': 'Asia/Seoul', 'tone': 'warm'}
        receipt = connection.execute('SELECT owner_id,idempotency_hash,fingerprint,export_bytes FROM data_privacy_requests WHERE id=%s', (accepted['id'],)).fetchone()
        assert receipt == dict.fromkeys(('owner_id', 'idempotency_hash', 'fingerprint', 'export_bytes'))
        assert connection.execute('SELECT 1 FROM data_privacy_requests WHERE id=%s', (export['id'],)).fetchone() is None
    assert service.get_data_request(accepted['id'], receipt=accepted['receipt'])['state'] == 'completed'
    with pytest.raises(DataNotFound):
        service.get_data_request(accepted['id'], owner=owners[0])
    with pytest.raises(DataNotFound):
        service.get_me(owners[0])


def test_invalid_erasure_and_idempotency_conflicts_do_not_start_deletion(data_context):
    client, current, service, owners, pool = data_context
    for confirmation in (None, '', 'delete', '내 기록 모두 삭제 '):
        body = {'kind': 'erase'}
        if confirmation is not None:
            body['confirmation'] = confirmation
        assert client.post('/v2/me/data-requests', json=body, headers={'X-Idempotency-Key': str(uuid4())}).status_code == 422
    assert client.get('/v2/me').status_code == 200
    assert client.post('/v2/me/data-requests', json={'kind': 'export'}).status_code == 422
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM data_privacy_requests WHERE owner_id=%s', (owners[0],)).fetchone()['n'] == 0


def test_worker_failure_rolls_back_erasure_and_reports_failed_before_explicit_retry(data_context):
    client, current, service, owners, pool = data_context
    seed_private_records(pool, owners)
    def unavailable(connection, owner):
        connection.execute('DELETE FROM journal_entries WHERE session_id=%s', (owner,))
        raise RuntimeError('external cleanup failed')
    service.erase_callbacks = (unavailable,)
    accepted = service.create_data_request(owners[0], 'erase', '내 기록 모두 삭제', uuid4())
    assert service.process_request(accepted['id']) is False
    assert service.get_data_request(accepted['id'], receipt=accepted['receipt'])['state'] == 'failed'
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM journal_entries WHERE session_id=%s', (owners[0],)).fetchone()['n'] == 1
        assert connection.execute('SELECT state FROM data_privacy_profiles WHERE owner_id=%s', (owners[0],)).fetchone()['state'] == 'deleting'
    service.erase_callbacks = ()
    assert service.process_request(accepted['id']) is True
    assert service.get_data_request(accepted['id'], receipt=accepted['receipt'])['state'] == 'completed'


def test_persisted_queue_can_resume_and_receipts_expire_by_policy(data_context):
    client, current, service, owners, pool = data_context
    request = service.create_data_request(owners[0], 'export', None, uuid4())
    assert service.process_pending() == 1
    assert service.get_data_request(request['id'], owner=owners[0])['state'] == 'completed'
    erase = service.create_data_request(owners[0], 'erase', '내 기록 모두 삭제', uuid4())
    service.process_request(erase['id'])
    later = service.clock() + timedelta(days=service.policy.receipt_days, seconds=1)
    service.clock = lambda: later
    with pytest.raises(DataExpired):
        service.get_data_request(erase['id'], receipt=erase['receipt'])
    service.purge_expired()
    with pool.connection() as connection:
        assert connection.execute('SELECT 1 FROM data_privacy_requests WHERE id=%s', (erase['id'],)).fetchone() is None
