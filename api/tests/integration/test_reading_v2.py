import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.data_privacy_postgres import PostgresDataPrivacyUnitOfWork, owner_guard
from api.adapters.http.reading_v2 import create_reading_v2_router
from api.adapters.reading_v2_postgres import PostgresReadingV2UnitOfWork
from api.application.data_privacy import DataPrivacyUseCases
from api.application.reading_v2 import ReadingV2Service
from api.domain.ai_jobs import AIError
from api.domain.data_privacy import DataPolicy

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


@pytest.fixture
def reading_env(pg_pool, owners):
    with pg_pool.connection() as c:
        for migration in sorted((ROOT / 'api/migrations').glob('*.sql')):
            c.execute(migration.read_text(encoding='utf-8'))
    catalog = json.loads((ROOT / 'shared/data-catalog.json').read_text(encoding='utf-8'))
    policy = DataPolicy.from_mapping(json.loads((ROOT / 'shared/data-policy.json').read_text(encoding='utf-8')))
    data = DataPrivacyUseCases(lambda: PostgresDataPrivacyUnitOfWork(pg_pool), policy, catalog)
    deck = data.list_decks()['items'][0]['id']
    clock = [datetime(2026, 11, 6, 15, 30, tzinfo=timezone.utc)]
    service = ReadingV2Service(lambda: PostgresReadingV2UnitOfWork(pg_pool), data,
                               owner_guard=owner_guard, clock=lambda: clock[0])
    current = [owners[0]]
    app = FastAPI()
    app.include_router(create_reading_v2_router(service, lambda: current[0]))
    with TestClient(app) as client:
        yield service, client, current, owners, pg_pool, data, deck, clock


def create(service, owner, deck, kind='three_card', key=None, **values):
    return service.create(owner, key or uuid4(), {'kind': kind, 'topic': 'care',
                                               'deck_version_id': deck, **values})


def test_three_card_pool_hides_card_ids_and_selection_preserves_order_and_labels(reading_env):
    service, client, _, owners, pool, data, deck, _ = reading_env
    owner = owners[0]
    r = create(service, owner, deck, question='내가 선택할 한 걸음')
    rid = UUID(r['id'])
    assert r['kind'] == 'three_card' and r['state'] == 'draft' and r['version'] == 1
    assert len(r['slots']) == 12 and r['selected_cards'] == [] and r['result'] is None
    assert all(set(slot) == {'id', 'index'} for slot in r['slots'])
    assert len({slot['id'] for slot in r['slots']}) == 12
    assert [slot['index'] for slot in r['slots']] == list(range(1, 13))
    with pytest.raises(AIError, match='READING_NOT_SELECTED'):
        service.basic(owner, rid)
    chosen = [r['slots'][8]['id'], r['slots'][2]['id'], r['slots'][5]['id']]
    with pool.connection() as c:
        private = c.execute('SELECT private_slots FROM p0_readings WHERE id=%s', (rid,)).fetchone()['private_slots']
        expected = {slot['id']: slot['card_id'] for slot in private}
    selected = service.select(owner, rid, uuid4(), 1, chosen)
    assert selected['version'] == 2 and selected['state'] == 'selected'
    assert [c['card_id'] for c in selected['selected_cards']] == [expected[s] for s in chosen]
    assert [c['position_label'] for c in selected['selected_cards']] == ['지금의 마음', '주변의 흐름', '다음 한 걸음']
    assert [c['position'] for c in selected['selected_cards']] == [1, 2, 3]
    assert all(c['orientation'] == 'upright' and c['card']['id'] == c['card_id'] for c in selected['selected_cards'])
    basic = service.basic(owner, rid)
    assert basic['source'] == 'basic' and basic['cards'] == selected['selected_cards']
    assert client.get(f'/v2/readings/{rid}/basic').status_code == 200


def test_shuffle_invalidates_all_slots_expected_version_and_only_one_selection_wins(reading_env):
    service, _, _, owners, _, _, deck, _ = reading_env
    owner = owners[0]
    r = create(service, owner, deck)
    rid = UUID(r['id'])
    shuffled = service.shuffle(owner, rid, uuid4(), 1)
    assert shuffled['version'] == 2 and not ({s['id'] for s in r['slots']} & {s['id'] for s in shuffled['slots']})
    for version, slots, code in [(1, [s['id'] for s in r['slots'][:3]], 'READING_VERSION_CONFLICT'),
                                 (2, [s['id'] for s in r['slots'][:3]], 'INVALID_SLOT_SELECTION'),
                                 (2, [shuffled['slots'][0]['id']] * 3, 'INVALID_SLOT_SELECTION')]:
        with pytest.raises(AIError, match=code):
            service.select(owner, rid, uuid4(), version, slots)
    def choose(offset):
        try:
            return service.select(owner, rid, uuid4(), 2, [s['id'] for s in shuffled['slots'][offset:offset + 3]])
        except AIError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(choose, [0, 3]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert 'READING_VERSION_CONFLICT' in results
    with pytest.raises(AIError, match='READING_STATE_CONFLICT'):
        service.shuffle(owner, rid, uuid4(), 3)


def test_daily_server_timezone_fixed_anchor_survives_deletion_and_settings_change(reading_env):
    service, _, _, owners, pool, data, deck, clock = reading_env
    owner = owners[0]
    data.update_preferences(owner, {'timezone': 'Asia/Seoul'})
    r = create(service, owner, deck, 'daily')
    assert r['local_day'] == '2026-11-07' and r['captured_timezone'] == 'Asia/Seoul'
    assert len(r['selected_cards']) == 1 and r['state'] == 'selected' and r['slots'] == []
    assert create(service, owner, deck, 'daily')['id'] == r['id']
    card = r['selected_cards'][0]['card_id']
    service.delete(owner, UUID(r['id']))
    replacement = create(service, owner, deck, 'daily')
    assert replacement['id'] != r['id'] and replacement['selected_cards'][0]['card_id'] == card
    data.update_preferences(owner, {'timezone': 'America/New_York'})
    assert service.get(owner, UUID(replacement['id']))['captured_timezone'] == 'Asia/Seoul'
    new_day = create(service, owner, deck, 'daily')
    assert new_day['local_day'] == '2026-11-06' and new_day['captured_timezone'] == 'America/New_York'
    with pytest.raises(AIError, match='READING_STATE_CONFLICT'):
        service.shuffle(owner, UUID(new_day['id']), uuid4(), new_day['version'])
    with pool.connection() as c:
        anchor = c.execute('SELECT card_id FROM p0_daily_draws WHERE owner_id=%s AND local_day=%s AND topic=%s', (owner, '2026-11-07', 'care')).fetchone()
        assert anchor['card_id'] == card


def test_idempotency_receipts_are_reference_only_and_deleted_replays_return_gone(reading_env):
    service, _, _, owners, pool, _, deck, _ = reading_env
    owner = owners[0]
    key = uuid4()
    r = create(service, owner, deck, key=key, question='private question text')
    assert create(service, owner, deck, key=key, question='private question text') == r
    with pytest.raises(AIError, match='IDEMPOTENCY_CONFLICT'):
        create(service, owner, deck, key=key, question='different')
    with pool.connection() as c:
        receipt = c.execute('SELECT * FROM p0_reading_commands WHERE owner_id=%s AND key=%s', (owner, key)).fetchone()
        assert receipt['reading_id'] == UUID(r['id'])
        assert 'private question text' not in json.dumps(receipt, default=str)
        assert not {'response', 'result', 'question', 'payload'} & set(receipt)
    service.delete(owner, UUID(r['id']))
    with pytest.raises(AIError, match='READING_DELETED') as error:
        create(service, owner, deck, key=key, question='private question text')
    assert error.value.status == 410


def test_owner_scope_catalog_validation_and_http_exact_shape(reading_env):
    service, client, current, owners, pool, data, deck, _ = reading_env
    headers = {'X-Idempotency-Key': str(uuid4())}
    body = {'kind': 'three_card', 'topic': 'care', 'deck_version_id': deck, 'question': '질문'}
    response = client.post('/v2/readings', headers=headers, json=body)
    assert response.status_code == 201
    r = response.json()
    assert set(r) == {'id', 'kind', 'topic', 'question', 'deck_version_id', 'local_day', 'captured_timezone', 'state', 'version', 'slots', 'selected_cards', 'result', 'created_at'}
    current[0] = owners[1]
    for path in [f"/v2/readings/{r['id']}", f"/v2/readings/{r['id']}/basic"]:
        assert client.get(path).status_code == 404
    assert client.get('/v2/readings').json()['items'] == []
    assert client.delete(f"/v2/readings/{r['id']}").status_code == 404
    assert client.post('/v2/readings', headers={'X-Idempotency-Key': str(uuid4())}, json={**body, 'question': 'a' * 501}).status_code == 422
    with pytest.raises(AIError, match='DECK_NOT_FOUND'):
        create(service, owners[0], str(uuid4()))
    current[0] = owners[0]
    data.catalog['decks'][0]['status'] = 'retired'
    with pytest.raises(AIError, match='DECK_UNAVAILABLE'):
        create(service, owners[0], deck)


def test_list_cursor_is_stable_scoped_and_rejects_filter_rebinding(reading_env):
    service, _, _, owners, pool, _, deck, _ = reading_env
    owner = owners[0]
    ids = [create(service, owner, deck)['id'] for _ in range(4)]
    with pool.connection() as c:
        c.execute("UPDATE p0_readings SET created_at='2026-11-06T00:00:00Z' WHERE owner_id=%s", (owner,))
    first = service.list(owner, limit=2, kind='three_card')
    second = service.list(owner, limit=2, kind='three_card', cursor=first['next_cursor'])
    assert [r['id'] for r in first['items'] + second['items']] == sorted(ids, reverse=True)
    assert second['next_cursor'] is None
    for who, kind in [(owners[1], 'three_card'), (owner, 'daily')]:
        with pytest.raises(AIError, match='INVALID_CURSOR'):
            service.list(who, limit=2, kind=kind, cursor=first['next_cursor'])


def test_active_owner_guards_and_erasure_remove_fixed_cards_and_commands(reading_env):
    service, _, _, owners, pool, data, deck, _ = reading_env
    owner = owners[0]
    r = create(service, owner, deck, 'daily')
    data.get_me(owner)
    with pool.connection() as c:
        c.execute("UPDATE data_privacy_profiles SET state='deleting' WHERE owner_id=%s", (owner,))
    with pytest.raises(AIError, match='OWNER_UNAVAILABLE'):
        service.get(owner, UUID(r['id']))
    with pool.connection() as c:
        service.erase_owner(owner, connection=c)
        for table in ('p0_readings', 'p0_daily_draws', 'p0_reading_commands'):
            assert c.execute(f'SELECT count(*) AS n FROM {table} WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
