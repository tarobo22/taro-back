import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from api.adapters.data_privacy_postgres import lock_owner, owner_guard
from api.adapters.http.data_privacy import data_privacy_router
from api.tests.integration.test_data_privacy_profile_catalog import data_context


pytestmark = pytest.mark.integration


def test_receipt_cannot_be_forged_from_known_public_request_metadata(data_context):
    client, current, service, owners, pool = data_context
    chosen_key = uuid4()
    request = service.create_data_request(owners[0], 'export', None, chosen_key)
    # Request IDs, profile IDs and idempotency keys are ordinary request metadata.
    # Possession of these alone must not create a receipt authorization capability.
    forged = hashlib.sha256(f"yeoun:data-receipt:v1:{owners[0]}:{request['id']}:{chosen_key}".encode()).hexdigest()
    current[0] = owners[1]
    assert client.get('/v2/data-requests/' + request['id'], headers={'X-Data-Receipt': forged}).status_code == 404
    assert client.get('/v2/data-requests/' + request['id'], headers={'X-Data-Receipt': request['receipt']}).status_code == 200


def test_erased_session_is_revoked_but_exact_receipt_still_polls_without_download_rights(data_context):
    _, current, service, owners, pool = data_context
    token = uuid4().hex + uuid4().hex
    with pool.connection() as connection:
        connection.execute('UPDATE app_sessions SET token_hash=%s WHERE id=%s',
                           (hashlib.sha256(token.encode()).hexdigest(), owners[0]))
    def authenticate(authorization: Annotated[str | None, Header()] = None):
        if authorization is None or not authorization.startswith('Bearer '):
            raise HTTPException(401)
        with pool.connection() as connection:
            row = connection.execute('SELECT id FROM app_sessions WHERE token_hash=%s',
                (hashlib.sha256(authorization[7:].encode()).hexdigest(),)).fetchone()
            if row is None or not owner_guard(connection, row['id']):
                raise HTTPException(401)
            return row['id']
    def status_owner(authorization: Annotated[str | None, Header()] = None,
                     receipt: Annotated[str | None, Header(alias='X-Data-Receipt')] = None):
        return None if receipt is not None else authenticate(authorization)
    app = FastAPI()
    app.include_router(data_privacy_router(service, authenticate, status_owner_dependency=status_owner))
    with TestClient(app) as client:
        auth = {'Authorization': 'Bearer ' + token, 'X-Idempotency-Key': str(uuid4())}
        assert client.get('/v2/me', headers=auth).status_code == 200
        accepted = client.post('/v2/me/data-requests', json={'kind': 'erase', 'confirmation': '내 기록 모두 삭제'}, headers=auth)
        assert accepted.status_code == 202 and accepted.json()['state'] == 'queued'
        status_path = '/v2/data-requests/' + accepted.json()['id']
        assert client.get('/v2/me', headers=auth).status_code == 401
        assert client.get(status_path, headers=auth).status_code == 401
        receipt = {'X-Data-Receipt': accepted.json()['receipt']}
        status = client.get(status_path, headers=receipt)
        assert status.status_code == 200 and status.json()['state'] == 'completed'
        assert status.json()['download_available'] is False
        assert client.get(status_path + '/download', headers=receipt).status_code == 401
        assert client.get(status_path, headers={'X-Data-Receipt': '0' * 64}).status_code == 404
        assert client.get(status_path + '?receipt=' + accepted.json()['receipt']).status_code == 401


def test_shared_owner_lock_prevents_late_sensitive_writer_from_resurrecting_erased_data(data_context):
    client, current, service, owners, pool = data_context
    entered = Event()
    release = Event()
    attempting_write = Event()
    def pause_erasure(connection, owner):
        entered.set()
        assert release.wait(5)
    service.erase_callbacks = (pause_erasure,)
    request = service.create_data_request(owners[0], 'erase', '내 기록 모두 삭제', uuid4())
    def delayed_writer():
        with pool.connection() as connection:
            attempting_write.set()
            lock_owner(connection, owners[0])
            if not owner_guard(connection, owners[0]):
                return 'discarded'
            connection.execute('''INSERT INTO journal_entries(id,session_id,day,mood,body)
                VALUES(%s,%s,'2026-11-06','calm','late result')''', (uuid4(), owners[0]))
            return 'committed'
    with ThreadPoolExecutor(max_workers=2) as executor:
        erasure = executor.submit(service.process_request, request['id'])
        try:
            assert entered.wait(5)
            writer = executor.submit(delayed_writer)
            assert attempting_write.wait(5)
            assert not writer.done()
        finally:
            release.set()
        assert erasure.result(timeout=5) is True
        assert writer.result(timeout=5) == 'discarded'
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM journal_entries WHERE session_id=%s', (owners[0],)).fetchone()['n'] == 0
