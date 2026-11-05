import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.adapters.data_privacy_postgres import PostgresDataPrivacyUnitOfWork
from api.adapters.http.data_privacy import data_privacy_router
from api.application.data_privacy import DataPrivacyUseCases
from api.domain.data_privacy import DataPolicy


pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[3]


@pytest.fixture
def data_context(pg_pool):
    with pg_pool.connection() as connection:
        for migration in sorted((ROOT / 'api/migrations').glob('*.sql')):
            connection.execute(migration.read_text(encoding='utf-8'))
    owners = (uuid4(), uuid4())
    with pg_pool.connection() as connection:
        for owner in owners:
            connection.execute('INSERT INTO app_sessions(id,token_hash) VALUES(%s,%s)', (owner, str(uuid4())))
    policy = DataPolicy.from_mapping(json.loads((ROOT / 'shared/data-policy.json').read_text(encoding='utf-8')))
    catalog = json.loads((ROOT / 'shared/data-catalog.json').read_text(encoding='utf-8'))
    service = DataPrivacyUseCases(lambda: PostgresDataPrivacyUnitOfWork(pg_pool), policy, catalog)
    current = [owners[0]]
    app = FastAPI()
    app.include_router(data_privacy_router(service, lambda: current[0]))
    with TestClient(app) as client:
        yield client, current, service, owners, pg_pool
    with pg_pool.connection() as connection:
        for table in ('experience_operations', 'journal_create_requests', 'journal_entries', 'tarot_readings',
                      'daily_readings', 'counsel_sessions'):
            connection.execute(f'DELETE FROM {table} WHERE session_id=ANY(%s)', (list(owners),))
        connection.execute('DELETE FROM app_sessions WHERE id=ANY(%s)', (list(owners),))


def test_preferences_are_persisted_and_isolated(data_context):
    client, current, service, owners, pool = data_context
    original = client.get('/v2/me')
    assert original.status_code == 200
    assert original.json() == {'id': str(owners[0]), 'kind': 'guest', 'locale': 'ko-KR',
                               'timezone': 'Asia/Seoul', 'preferences': {'tone': 'warm',
                               'reduced_motion': False, 'voice_mode': 'device'}}
    updated = client.patch('/v2/me/preferences', json={'locale': 'en-US', 'timezone': 'America/New_York',
                            'tone': 'giga', 'reduced_motion': True, 'voice_mode': 'cloud'})
    assert updated.status_code == 200
    assert client.get('/v2/me').json() == updated.json()
    current[0] = owners[1]
    assert client.get('/v2/me').json()['preferences'] == original.json()['preferences']
    assert client.get('/v2/me').json()['timezone'] == 'Asia/Seoul'
    # A new service instance reads persisted state, not request-local memory.
    assert service.get_me(owners[0])['timezone'] == 'America/New_York'


@pytest.mark.parametrize('patch', [{}, {'timezone': 'Mars/Olympus'}, {'timezone': '../UTC'},
    {'timezone': '+09:00'}, {'locale': 'not-a-locale'}, {'tone': 'harsh'},
    {'voice_mode': 'always-record'}, {'reduced_motion': 'yes'}, {'tone': None}, {'extra': True}])
def test_invalid_preferences_never_change_stored_state(data_context, patch):
    client, *_ = data_context
    before = client.get('/v2/me').json()
    assert client.patch('/v2/me/preferences', json=patch).status_code == 422
    assert client.get('/v2/me').json() == before


def test_catalog_has_all_78_stable_cards_and_honest_asset_metadata(data_context):
    client, *_ = data_context
    decks = client.get('/v2/decks').json()
    assert decks['next_cursor'] is None and len(decks['items']) == 1
    deck = decks['items'][0]
    UUID(deck['id'])
    assert deck['status'] == 'candidate'  # Existing RWS assets have prototype_candidate review only.
    assert deck['card_count'] == 78
    assert client.get('/v2/decks/' + deck['id']).json() == deck
    cards = client.get('/v2/cards').json()
    assert cards['next_cursor'] is None
    assert [card['id'] for card in cards['items']] == list(range(78))
    assert len({card['code'] for card in cards['items']}) == 78
    for card in cards['items']:
        assert card['basic_meaning'] and card['name_ko'] and card['name_en']
        assert len(card['image_asset']['sha256']) == 64
        assert card['image_asset']['width'] > 0 and card['image_asset']['height'] > 0
        assert card['image_asset']['source_page'].startswith('https://commons.wikimedia.org/wiki/File:')
        assert card['image_asset']['license_name'] == 'Public domain'
        assert client.get('/v2/cards/' + str(card['id'])).json() == card
    assert client.get('/v2/cards?arcana=major').json()['items'] == cards['items'][:22]
    assert len(client.get('/v2/cards?suit=cups').json()['items']) == 14
    assert client.get('/v2/cards?deck_id=' + deck['id']).json() == cards
    assert client.get('/v2/cards?deck_id=' + str(uuid4())).status_code == 404
    assert client.get('/v2/cards/78').status_code == 404
    assert client.get('/v2/cards/-1').status_code == 404
    assert client.get('/v2/decks/' + str(uuid4())).status_code == 404


def test_deleting_or_erased_owner_cannot_change_preferences(data_context):
    client, current, service, owners, pool = data_context
    client.get('/v2/me')
    with pool.connection() as connection:
        connection.execute("UPDATE data_privacy_profiles SET state='deleting' WHERE owner_id=%s", (owners[0],))
    assert client.patch('/v2/me/preferences', json={'tone': 'giga'}).status_code == 409
    with pool.connection() as connection:
        connection.execute("UPDATE data_privacy_profiles SET state='erased' WHERE owner_id=%s", (owners[0],))
    assert client.get('/v2/me').status_code == 404
