import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.http.journal import journal_router
from api.adapters.http.journal_v2 import journal_v2_router
from api.adapters.journal_v2_postgres import PostgresJournalV2UnitOfWork
from api.adapters.postgres import PostgresJournalUnitOfWork
from api.application.journal import JournalUseCases
from api.application.journal_v2 import JournalV2UseCases
from api.domain.journal import JournalPolicy
from api.domain.journal_v2 import JournalV2Policy
from api.tests.integration.test_data_privacy_profile_catalog import data_context


pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


@pytest.fixture
def journal_v2(data_context):
    _, current, privacy, owners, pool = data_context
    content = json.loads((ROOT / 'shared/content.json').read_text(encoding='utf-8'))
    rules = json.loads((ROOT / 'shared/journal-policy.json').read_text(encoding='utf-8'))
    extra = json.loads((ROOT / 'shared/journal-v2-policy.json').read_text(encoding='utf-8'))
    policy = JournalV2Policy.from_legacy(content['moods'], rules, extra)
    sources = {'reading': {}, 'turn': {}}
    invalidated = []
    def source_exists(connection, owner, kind, source_id):
        return sources[kind].get(source_id) == owner
    def invalidate(connection, owner, entry_id):
        assert connection.execute('SELECT 1 FROM journal_entries WHERE session_id=%s AND id=%s',
                                  (owner, entry_id)).fetchone()
        invalidated.append((owner, entry_id))
    service = JournalV2UseCases(lambda: PostgresJournalV2UnitOfWork(pool), policy,
                                source_exists=source_exists, invalidate_context=invalidate)
    legacy_policy = JournalPolicy(frozenset(content['moods']), rules['maxBodyLength'])
    legacy = JournalUseCases(lambda: PostgresJournalUnitOfWork(pool), legacy_policy, uuid4)
    app = FastAPI()
    app.include_router(journal_v2_router(service, lambda: current[0]))
    app.include_router(journal_router(legacy, legacy_policy, lambda: current[0]))
    with TestClient(app) as client:
        yield client, current, service, owners, pool, sources, invalidated, privacy


def create(client, body=None, key=None):
    return client.post('/v2/journal-entries', json=body or {'local_day': '2026-11-06', 'mood': 'calm', 'body': '내 기록'},
                       headers={'X-Idempotency-Key': key or str(uuid4())})


def test_journal_v2_uses_existing_records_and_translates_moods_both_directions(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    created = create(client)
    assert created.status_code == 201
    entry = created.json()
    assert entry['local_day'] == '2026-11-06' and entry['mood'] == 'calm' and entry['version'] == 1
    assert entry['source_reading_id'] is entry['source_turn_id'] is None
    path = '/v2/journal-entries/' + entry['id']
    legacy = client.get('/v1/entries/' + entry['id']).json()
    assert legacy['mood'] == '편안해요' and legacy['body'] == entry['body']
    assert client.put('/v1/entries/' + entry['id'], json={'mood': '기뻐요', 'body': 'v1 edit', 'expectedVersion': 1}).status_code == 200
    current_v2 = client.get(path).json()
    assert current_v2['version'] == 2 and current_v2['mood'] == 'happy' and current_v2['body'] == 'v1 edit'
    changed = client.patch(path, json={'expected_version': 2, 'mood': 'thinking', 'body': 'v2 edit'})
    assert changed.status_code == 200 and changed.json()['version'] == 3
    assert client.get('/v1/entries/' + entry['id']).json()['mood'] == '생각이 많아요'
    assert changed.json()['local_day'] == entry['local_day']
    current[0] = owners[1]
    assert client.get(path).status_code == 404
    assert client.patch(path, json={'expected_version': 3, 'body': 'other owner'}).status_code == 404
    assert client.delete(path).status_code == 404
    assert client.get('/v2/journal-entries').json()['items'] == []


def test_journal_creation_idempotency_stores_only_references_and_never_resurrects_deletions(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    key = str(uuid4())
    body = {'local_day': '2026-11-06', 'mood': 'talk', 'body': 'private diary text'}
    first = create(client, body, key)
    assert first.status_code == 201 and create(client, body, key).json() == first.json()
    assert create(client, {**body, 'body': 'changed'}, key).status_code == 409
    entry_id = first.json()['id']
    with pool.connection() as connection:
        columns = {r['column_name'] for r in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name='journal_v2_create_requests'")}
        assert not columns & {'body', 'result', 'payload', 'snapshot'}
        assert connection.execute('SELECT count(*) AS n FROM journal_entries WHERE session_id=%s', (owners[0],)).fetchone()['n'] == 1
    path = '/v2/journal-entries/' + entry_id
    assert client.patch(path, json={'expected_version': 1, 'body': 'updated'}).status_code == 200
    assert create(client, body, key).json()['body'] == 'updated'
    assert client.delete(path).status_code == 204
    assert client.delete(path).status_code == 204
    assert create(client, body, key).status_code == 410
    assert client.get(path).status_code == 404
    assert len(invalidated) == 1
    assert client.delete('/v2/journal-entries/' + str(uuid4())).status_code == 404


def test_journal_source_links_are_mutually_exclusive_and_owner_scoped(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    reading_id, other_reading, turn_id = uuid4(), uuid4(), uuid4()
    sources['reading'].update({reading_id: owners[0], other_reading: owners[1]})
    sources['turn'][turn_id] = owners[0]
    base = {'local_day': '2026-11-06', 'mood': 'settled'}
    linked = create(client, {**base, 'source_reading_id': str(reading_id)})
    assert linked.status_code == 201 and linked.json()['source_reading_id'] == str(reading_id)
    assert create(client, {**base, 'source_turn_id': str(turn_id)}).status_code == 201
    assert create(client, {**base, 'source_reading_id': str(other_reading)}).status_code == 404
    assert create(client, {**base, 'source_reading_id': str(uuid4())}).status_code == 404
    assert create(client, {**base, 'source_reading_id': str(reading_id), 'source_turn_id': str(turn_id)}).status_code == 422
    detail = client.get('/v2/journal-entries/' + linked.json()['id']).json()
    assert detail['source_reading_id'] == str(reading_id)


@pytest.mark.parametrize('patch', [{}, {'body': 'missing version'}, {'expected_version': 1},
    {'expected_version': 0, 'body': 'invalid'}, {'expected_version': True, 'body': 'invalid'},
    {'expected_version': 1, 'mood': None}, {'expected_version': 1, 'body': None},
    {'expected_version': 1, 'local_day': '2026-11-07'},
    {'expected_version': 1, 'body': 'x' * 4001}])
def test_journal_patch_requires_a_valid_version_and_changed_field_without_mutating(journal_v2, patch):
    client, *_ = journal_v2
    original = create(client).json()
    path = '/v2/journal-entries/' + original['id']
    assert client.patch(path, json=patch).status_code == 422
    assert client.get(path).json() == original


def test_concurrent_journal_edits_allow_one_version_and_preserve_the_winner(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    entry = create(client).json()
    def update(text):
        return client.patch('/v2/journal-entries/' + entry['id'], json={'expected_version': 1, 'body': text})
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(update, ('first editor', 'second editor')))
    assert sorted(response.status_code for response in results) == [200, 409]
    winner = next(response.json() for response in results if response.status_code == 200)
    assert winner['version'] == 2
    assert client.get('/v2/journal-entries/' + entry['id']).json() == winner


def test_journal_keyset_pages_are_stable_and_bound_to_owner_and_date_mood_filters(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    entries = [create(client, {'local_day': '2026-11-05' if n % 2 else '2026-11-06',
                              'mood': 'happy' if n % 2 else 'calm', 'body': str(n)}).json() for n in range(6)]
    with pool.connection() as connection:
        connection.execute("UPDATE journal_entries SET created_at='2026-11-06T00:00:00Z' WHERE session_id=%s", (owners[0],))
    collected = []
    params = {'limit': 2}
    first_cursor = None
    while True:
        page = client.get('/v2/journal-entries', params=params)
        assert page.status_code == 200
        collected.extend(row['id'] for row in page.json()['items'])
        cursor = page.json()['next_cursor']
        if first_cursor is None:
            first_cursor = cursor
            # A new record sorting before this cursor must not shift later pages.
            newer = create(client, {'local_day': '2026-11-07', 'mood': 'calm', 'body': 'newly inserted'}).json()
            with pool.connection() as connection:
                connection.execute("UPDATE journal_entries SET created_at='2026-11-07T00:00:00Z' WHERE id=%s", (newer['id'],))
        if cursor is None:
            break
        params['cursor'] = cursor
    assert collected == sorted([row['id'] for row in entries], reverse=True)
    filtered = client.get('/v2/journal-entries', params={'from': '2026-11-05', 'to': '2026-11-05', 'mood': 'happy'}).json()
    assert len(filtered['items']) == 3 and all(row['mood'] == 'happy' for row in filtered['items'])
    assert client.get('/v2/journal-entries', params={'cursor': first_cursor, 'mood': 'happy'}).status_code == 422
    assert client.get('/v2/journal-entries', params={'from': '2026-11-06', 'to': '2026-11-05'}).status_code == 422
    assert client.get('/v2/journal-entries', params={'limit': 51}).status_code == 422
    assert client.get('/v2/journal-entries', params={'cursor': 'bad'}).status_code == 422
    current[0] = owners[1]
    assert client.get('/v2/journal-entries', params={'cursor': first_cursor}).status_code == 422


def test_journal_delete_callback_failure_keeps_entry_and_privacy_erasure_removes_metadata(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    entry = create(client).json()
    def unavailable(connection, owner, entry_id):
        raise RuntimeError('cleanup unavailable')
    service.invalidate_context = unavailable
    with pytest.raises(RuntimeError, match='cleanup unavailable'):
        service.delete(owners[0], entry['id'])
    assert client.get('/v2/journal-entries/' + entry['id']).status_code == 200
    privacy.export_callbacks = (service.export_owner,)
    privacy.erase_callbacks = (service.erase_owner,)
    erase = privacy.create_data_request(owners[0], 'erase', '내 기록 모두 삭제', uuid4())
    assert privacy.process_request(erase['id']) is True
    with pool.connection() as connection:
        for table in ('journal_v2_sources', 'journal_v2_create_requests', 'journal_v2_deleted_entries'):
            assert connection.execute(f'SELECT count(*) AS n FROM {table} WHERE owner_id=%s', (owners[0],)).fetchone()['n'] == 0


def test_journal_validates_create_lengths_identity_and_owner_lifecycle(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    for body in ({'local_day': '2026-11-06', 'mood': 'angry'}, {'local_day': 'bad', 'mood': 'calm'},
                 {'local_day': '2026-11-06', 'mood': 'calm', 'body': 'x' * 4001},
                 {'local_day': '2026-11-06', 'mood': 'calm', 'extra': True}):
        assert create(client, body).status_code == 422
    assert client.post('/v2/journal-entries', json={'local_day': '2026-11-06', 'mood': 'calm'}).status_code == 422
    privacy.get_me(owners[0])
    with pool.connection() as connection:
        connection.execute("UPDATE data_privacy_profiles SET state='deleting' WHERE owner_id=%s", (owners[0],))
    assert create(client).status_code == 409


def test_expired_create_key_drops_payload_hash_and_reference_without_recreating_diary(journal_v2):
    client, current, service, owners, pool, sources, invalidated, privacy = journal_v2
    key = str(uuid4())
    first = create(client, key=key)
    assert first.status_code == 201
    later = service.clock() + timedelta(hours=service.policy.idempotency_hours, seconds=1)
    service.clock = lambda: later
    service.purge_expired_idempotency()
    assert create(client, key=key).status_code == 410
    assert client.get('/v2/journal-entries/' + first.json()['id']).status_code == 200
    with pool.connection() as connection:
        operation = connection.execute('SELECT fingerprint,entry_id FROM journal_v2_create_requests WHERE owner_id=%s', (owners[0],)).fetchone()
        assert operation == {'fingerprint': None, 'entry_id': None}
        assert connection.execute('SELECT count(*) AS n FROM journal_entries WHERE session_id=%s', (owners[0],)).fetchone()['n'] == 1
