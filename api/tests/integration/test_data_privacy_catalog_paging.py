from uuid import uuid4

import pytest

from api.tests.integration.test_data_privacy_profile_catalog import data_context


pytestmark = pytest.mark.integration


def test_catalog_pagination_covers_all_cards_once_and_validates_version(data_context):
    client, current, service, owners, pool = data_context
    deck_id = service.list_decks()['items'][0]['id']
    first = client.get('/v2/cards', params={'deck_version_id': deck_id})
    assert first.status_code == 200 and len(first.json()['items']) == 20
    cards = []
    cursor = None
    while True:
        params = {'deck_version_id': deck_id, 'limit': 17}
        if cursor:
            params['cursor'] = cursor
        page = client.get('/v2/cards', params=params)
        assert page.status_code == 200
        cards.extend(page.json()['items'])
        cursor = page.json()['next_cursor']
        if cursor is None:
            break
    assert [card['id'] for card in cards] == list(range(78))
    assert client.get('/v2/cards/0', params={'deck_version_id': deck_id}).json() == cards[0]
    assert client.get('/v2/cards/0', params={'deck_version_id': str(uuid4())}).status_code == 404
    assert client.get('/v2/cards', params={'deck_version_id': str(uuid4())}).status_code == 404
    assert client.get('/v2/cards', params={'deck_version_id': deck_id, 'deck_id': str(uuid4())}).status_code == 422
    # Domain callers used by reading selection still receive the complete catalog.
    assert len(service.list_cards()['items']) == 78


@pytest.mark.parametrize('path', ['/v2/cards', '/v2/decks'])
def test_catalog_rejects_invalid_limits_and_cursors(data_context, path):
    client, *_ = data_context
    for limit in (0, -1, 51, 1000, 'invalid'):
        assert client.get(path, params={'limit': limit}).status_code == 422
    assert client.get(path, params={'cursor': 'not a valid cursor'}).status_code == 422
    assert client.get(path, params={'limit': 50}).status_code == 200


def test_catalog_cursor_is_bound_to_kind_version_and_filters(data_context):
    client, *_ = data_context
    cursor = client.get('/v2/cards', params={'limit': 1}).json()['next_cursor']
    assert cursor
    assert client.get('/v2/decks', params={'cursor': cursor}).status_code == 422
    assert client.get('/v2/cards', params={'cursor': cursor, 'suit': 'cups'}).status_code == 422
