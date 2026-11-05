from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from threading import Event
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.errors import ForeignKeyViolation

from api.adapters.data_privacy_postgres import PostgresDataPrivacyUnitOfWork, lock_owner, owner_guard
from api.adapters.favorites_postgres import PostgresFavoritesRepository, PostgresFavoritesUnitOfWork
from api.adapters.http.favorites import favorites_router
from api.adapters.reading_v2_postgres import PostgresReadingV2UnitOfWork
from api.application.data_privacy import DataPrivacyUseCases
from api.application.favorites import FavoritesUseCases
from api.application.reading_v2 import ReadingV2Service
from api.domain.data_privacy import DataNotFound, DataPolicy, OwnerDeleting
from api.domain.favorites import FavoriteNotFound


pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


@pytest.fixture
def favorite_data(pg_pool, owners):
    with pg_pool.connection() as connection:
        for migration in sorted((ROOT / 'api/migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    favorites = FavoritesUseCases(lambda: PostgresFavoritesUnitOfWork(pg_pool))
    readings = ReadingV2Service(lambda: PostgresReadingV2UnitOfWork(pg_pool), None, owner_guard=owner_guard)
    current = [owners[0]]
    app = FastAPI()
    app.include_router(favorites_router(favorites, lambda: current[0]))
    with TestClient(app) as client:
        yield client, current, owners, pg_pool, favorites, readings


def seed_reading(pool, owner, *, reading_id=None):
    reading_id = reading_id or uuid4()
    with pool.connection() as connection:
        connection.execute('''INSERT INTO p0_readings(id,owner_id,kind,topic,deck_version_id,captured_timezone,state)
            VALUES(%s,%s,'three_card','care',%s,'Asia/Seoul','draft')''', (reading_id, owner, uuid4()))
    return reading_id


def add(client, reading, key=None):
    return client.post('/v2/favorites', json={'reading_id': str(reading)}, headers={'X-Idempotency-Key': str(key or uuid4())})


def privacy_service(pool, favorites, readings, *, export_extra=(), erase_extra=()):
    policy = DataPolicy.from_mapping(json.loads((ROOT / 'shared/data-policy.json').read_text(encoding='utf-8')))
    catalog = json.loads((ROOT / 'shared/data-catalog.json').read_text(encoding='utf-8'))
    return DataPrivacyUseCases(lambda: PostgresDataPrivacyUnitOfWork(pool), policy, catalog,
        export_callbacks=(readings.export_owner, favorites.export_owner, *export_extra),
        erase_callbacks=(readings.erase_owner, favorites.erase_owner, *erase_extra))


def test_favorite_owner_scope_duplicate_and_original_response_idempotency(favorite_data):
    client, current, owners, pool, _, _ = favorite_data
    mine, foreign = seed_reading(pool, owners[0]), seed_reading(pool, owners[1])
    key = uuid4()
    first = add(client, mine, key)
    assert first.status_code == 201 and set(first.json()) == {'reading_id', 'created_at'}
    assert add(client, mine, key).json() == first.json()
    assert add(client, mine).json() == first.json()
    assert add(client, foreign, key).status_code == 409
    assert add(client, foreign).status_code == add(client, uuid4()).status_code == 404
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM favorites WHERE owner_id=%s', (owners[0],)).fetchone()['n'] == 1
        columns = {r['column_name'] for r in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name='p1_favorite_create_requests'")}
        assert not columns & {'body', 'result', 'payload', 'response', 'snapshot', 'question'}
    current[0] = owners[1]
    assert client.get('/v2/favorites').json() == {'items': [], 'next_cursor': None}
    assert client.delete('/v2/favorites/' + str(mine)).status_code == 404
    assert add(client, mine).status_code == 404


def test_paging_same_timestamp_tie_break_cursor_binding_and_live_removal(favorite_data):
    client, current, owners, pool, _, _ = favorite_data
    ids = [seed_reading(pool, owners[0]) for _ in range(4)]
    for reading in ids:
        assert add(client, reading).status_code == 201
    with pool.connection() as connection:
        connection.execute('UPDATE favorites SET created_at=%s WHERE owner_id=%s', (datetime(2026, 11, 6, tzinfo=timezone.utc), owners[0]))
    first = client.get('/v2/favorites?limit=2').json()
    assert [row['reading_id'] for row in first['items']] == [str(i) for i in sorted(ids, reverse=True)[:2]]
    assert first['next_cursor']
    second = client.get('/v2/favorites', params={'limit': 2, 'cursor': first['next_cursor']}).json()
    assert [row['reading_id'] for row in second['items']] == [str(i) for i in sorted(ids, reverse=True)[2:]]
    assert second['next_cursor'] is None
    current[0] = owners[1]
    assert client.get('/v2/favorites', params={'cursor': first['next_cursor']}).status_code == 422
    current[0] = owners[0]
    removed = first['items'][0]['reading_id']
    assert client.delete('/v2/favorites/' + removed).status_code == 204
    assert client.delete('/v2/favorites/' + removed).status_code == 404
    assert removed not in {r['reading_id'] for r in client.get('/v2/favorites').json()['items']}
    assert client.get('/v2/favorites?cursor=invalid!').status_code == 422


def test_deleting_a_source_cascades_favorites_and_spends_replay_key(favorite_data):
    client, _, owners, pool, _, readings = favorite_data
    source, replacement = seed_reading(pool, owners[0]), seed_reading(pool, owners[0])
    key = uuid4()
    assert add(client, source, key).status_code == 201
    readings.delete(owners[0], source)
    assert client.get('/v2/favorites').json() == {'items': [], 'next_cursor': None}
    assert add(client, source, key).status_code == 404
    assert add(client, replacement, key).status_code == 409
    assert add(client, source).status_code == 404
    with pool.connection() as connection:
        assert connection.execute('SELECT reading_id FROM p1_favorite_create_requests WHERE owner_id=%s', (owners[0],)).fetchone()['reading_id'] is None
        assert connection.execute('SELECT count(*) AS n FROM favorites WHERE owner_id=%s', (owners[0],)).fetchone()['n'] == 0


def test_removed_favorite_is_not_recreated_by_replay_but_a_new_key_can_add(favorite_data):
    client, _, owners, pool, _, _ = favorite_data
    reading, key = seed_reading(pool, owners[0]), uuid4()
    original = add(client, reading, key).json()
    assert client.delete('/v2/favorites/' + str(reading)).status_code == 204
    assert add(client, reading, key).status_code == 404
    recreated = add(client, reading)
    assert recreated.status_code == 201
    # Original request metadata survives a subsequent re-add and returns the original DTO.
    assert add(client, reading, key).json() == original
    assert recreated.json()['created_at'] != original['created_at']


def test_database_foreign_key_also_rejects_an_owner_mismatch(favorite_data):
    _, _, owners, pool, _, _ = favorite_data
    foreign = seed_reading(pool, owners[1])
    with pytest.raises(ForeignKeyViolation):
        with pool.connection() as connection:
            connection.execute('INSERT INTO favorites(owner_id,reading_id) VALUES(%s,%s)', (owners[0], foreign))


def test_concurrent_same_and_distinct_keys_never_duplicate_favorites(favorite_data):
    _, _, owners, pool, favorites, _ = favorite_data
    reading, key = seed_reading(pool, owners[0]), uuid4()
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(favorites.add, owners[0], {'reading_id': reading}, candidate)
                   for candidate in (key, key, uuid4(), uuid4())]
        results = [future.result(timeout=10) for future in futures]
    assert results == [results[0]] * 4
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM favorites WHERE owner_id=%s', (owners[0],)).fetchone()['n'] == 1
        assert connection.execute('SELECT count(*) AS n FROM p1_favorite_create_requests WHERE owner_id=%s', (owners[0],)).fetchone()['n'] == 3


@pytest.mark.parametrize('state,status', [('deleting', 409), ('erased', 404)])
def test_every_favorite_endpoint_rejects_inactive_owner(favorite_data, state, status):
    client, _, owners, pool, _, _ = favorite_data
    reading = seed_reading(pool, owners[0])
    assert add(client, reading).status_code == 201
    with pool.connection() as connection:
        connection.execute('INSERT INTO data_privacy_profiles(owner_id,state) VALUES(%s,%s)', (owners[0], state))
    assert client.get('/v2/favorites').status_code == status
    assert add(client, reading).status_code == status
    assert client.delete('/v2/favorites/' + str(reading)).status_code == status


def test_privacy_export_has_unique_public_dataset_and_full_erasure_clears_favorites_and_replay_metadata(favorite_data):
    client, _, owners, pool, favorites, readings = favorite_data
    mine, foreign = seed_reading(pool, owners[0]), seed_reading(pool, owners[1])
    assert add(client, mine).status_code == 201
    favorites.add(owners[1], {'reading_id': foreign}, uuid4())
    privacy = privacy_service(pool, favorites, readings)
    request = privacy.create_data_request(owners[0], 'export', None, uuid4())
    assert privacy.process_request(request['id']) is True
    archive = privacy.download_export(owners[0], request['id'])
    with ZipFile(io.BytesIO(archive)) as export:
        data = json.loads(export.read('data.json'))
        assert list(data).count('v2_favorites') == 1
        assert data['v2_favorites'] == favorites.list(owners[0])['items']
        assert data['v2_favorites'][0]['reading_id'] == client.get('/v2/favorites').json()['items'][0]['reading_id']
        assert set(data['v2_favorites'][0]) == {'reading_id', 'created_at'}
        assert str(foreign) not in json.dumps(data['v2_favorites'])
    erase = privacy.create_data_request(owners[0], 'erase', privacy.policy.erase_confirmation, uuid4())
    assert privacy.process_request(erase['id']) is True
    with pool.connection() as connection:
        for table in ('favorites', 'p1_favorite_create_requests', 'p0_readings'):
            assert not connection.execute(f'SELECT 1 FROM {table} WHERE owner_id=%s', (owners[0],)).fetchone()
        assert connection.execute('SELECT 1 FROM favorites WHERE owner_id=%s', (owners[1],)).fetchone()
    assert client.get('/v2/favorites').status_code == 404


def test_privacy_callbacks_participate_in_caller_transaction_and_rollback(favorite_data):
    client, _, owners, pool, favorites, _ = favorite_data
    reading = seed_reading(pool, owners[0])
    assert add(client, reading).status_code == 201
    expected = favorites.list(owners[0])['items'][0]
    with pytest.raises(RuntimeError, match='caller rollback'):
        with pool.connection() as connection:
            assert favorites.export_owner(connection, owners[0]) == {'v2_favorites': [expected]}
            favorites.erase_owner(connection, owners[0])
            assert not connection.execute('SELECT 1 FROM favorites WHERE owner_id=%s', (owners[0],)).fetchone()
            raise RuntimeError('caller rollback')
    assert favorites.list(owners[0])['items'] == [expected]


def test_source_delete_holds_owner_lock_so_queued_favorite_cannot_recreate_it(favorite_data):
    _, _, owners, pool, favorites, readings = favorite_data
    reading = seed_reading(pool, owners[0])
    started = Event()
    def queued_add():
        started.set()
        return favorites.add(owners[0], {'reading_id': reading}, uuid4())
    with ThreadPoolExecutor(max_workers=1) as executor:
        with pool.connection() as connection:
            lock_owner(connection, owners[0])
            # Use the real reading service on the same transaction under deletion's lock.
            readings._repository_for(connection).delete(owners[0], reading)
            future = executor.submit(queued_add)
            assert started.wait(2)
        with pytest.raises(FavoriteNotFound):
            future.result(timeout=10)
    assert favorites.list(owners[0])['items'] == []


def test_favorite_insert_holds_owner_lock_until_commit_and_source_delete_then_cascades(favorite_data):
    _, _, owners, pool, favorites, readings = favorite_data
    reading = seed_reading(pool, owners[0])
    inserted, release = Event(), Event()
    class PausingFavoritesRepository(PostgresFavoritesRepository):
        def add(self, owner, reading_id):
            result = super().add(owner, reading_id)
            inserted.set()
            assert release.wait(5)
            return result
    class PausingUnit(PostgresFavoritesUnitOfWork):
        def repository_for(self, transaction):
            return PausingFavoritesRepository(transaction)
    paused = FavoritesUseCases(lambda: PausingUnit(pool))
    with ThreadPoolExecutor(max_workers=2) as executor:
        adding = executor.submit(paused.add, owners[0], {'reading_id': reading}, uuid4())
        assert inserted.wait(2)
        deleting = executor.submit(readings.delete, owners[0], reading)
        release.set()
        assert adding.result(timeout=10)['reading_id'] == str(reading)
        deleting.result(timeout=10)
    assert favorites.list(owners[0])['items'] == []


def test_full_erasure_wins_owner_lock_and_queued_favorite_request_is_rejected(favorite_data):
    _, _, owners, pool, favorites, readings = favorite_data
    reading = seed_reading(pool, owners[0])
    favorites.add(owners[0], {'reading_id': reading}, uuid4())
    erased, release, requested = Event(), Event(), Event()
    def pause_erase(connection, owner):
        erased.set()
        assert release.wait(5)
    privacy = privacy_service(pool, favorites, readings, erase_extra=(pause_erase,))
    erase = privacy.create_data_request(owners[0], 'erase', privacy.policy.erase_confirmation, uuid4())
    def queued_add():
        requested.set()
        return favorites.add(owners[0], {'reading_id': reading}, uuid4())
    with ThreadPoolExecutor(max_workers=2) as executor:
        erasing = executor.submit(privacy.process_request, erase['id'])
        assert erased.wait(2)
        adding = executor.submit(queued_add)
        assert requested.wait(2)
        release.set()
        assert erasing.result(timeout=10) is True
        with pytest.raises(DataNotFound):
            adding.result(timeout=10)
    with pool.connection() as connection:
        for table in ('favorites', 'p1_favorite_create_requests'):
            assert not connection.execute(f'SELECT 1 FROM {table} WHERE owner_id=%s', (owners[0],)).fetchone()
