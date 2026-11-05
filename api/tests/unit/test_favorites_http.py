from datetime import datetime, timezone
from unittest.mock import Mock
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.adapters.http.favorites import favorites_router
from api.domain.data_privacy import DataNotFound, OwnerDeleting
from api.domain.favorites import FavoriteConflict, FavoriteInvalid, FavoriteNotFound, FavoritesPolicy


OWNER, READING, KEY = (UUID(int=i) for i in range(1, 4))
FAVORITE = {'reading_id': str(READING), 'created_at': datetime(2026, 11, 6, tzinfo=timezone.utc).isoformat()}


def context(owner=lambda: OWNER):
    service = Mock()
    service.policy = FavoritesPolicy()
    service.add.return_value = FAVORITE
    service.list.return_value = {'items': [FAVORITE], 'next_cursor': None}
    app = FastAPI()
    app.include_router(favorites_router(service, owner))
    return service, TestClient(app)


def test_favorites_http_contract_methods_and_uuid_request_header():
    service, client = context()
    with client:
        created = client.post('/v2/favorites', json={'reading_id': str(READING)}, headers={'X-Idempotency-Key': str(KEY)})
        assert created.status_code == 201 and created.json()['reading_id'] == str(READING)
        assert set(created.json()) == {'reading_id', 'created_at'}
        service.add.assert_called_once_with(OWNER, {'reading_id': READING}, KEY)
        listing = client.get('/v2/favorites')
        assert listing.status_code == 200 and listing.json()['items'] == [created.json()]
        service.list.assert_called_once_with(OWNER, cursor=None, limit=20)
        deleted = client.delete('/v2/favorites/' + str(READING))
        assert deleted.status_code == 204 and deleted.content == b''
        service.remove.assert_called_once_with(OWNER, READING)
        paths = client.get('/openapi.json').json()['paths']
        assert paths['/v2/favorites']['get']['operationId'] == 'listFavorites'
        assert paths['/v2/favorites']['post']['operationId'] == 'addFavorite'
        assert paths['/v2/favorites/{reading_id}']['delete']['operationId'] == 'removeFavorite'
        request_header = next(p for p in paths['/v2/favorites']['post']['parameters'] if p['in'] == 'header')
        assert request_header['name'] == 'X-Idempotency-Key' and request_header['required']


@pytest.mark.parametrize('body,headers', [({}, {'X-Idempotency-Key': str(KEY)}),
    ({'reading_id': 'not-uuid'}, {'X-Idempotency-Key': str(KEY)}),
    ({'reading_id': str(READING), 'extra': True}, {'X-Idempotency-Key': str(KEY)}),
    ({'reading_id': str(READING)}, {}), ({'reading_id': str(READING)}, {'X-Idempotency-Key': 'invalid'})])
def test_invalid_create_body_or_header_does_not_reach_service(body, headers):
    service, client = context()
    with client:
        assert client.post('/v2/favorites', json=body, headers=headers).status_code == 422
    service.add.assert_not_called()


@pytest.mark.parametrize('query', ['limit=0', 'limit=51', 'limit=1.5', 'cursor=' + 'x' * 1025])
def test_invalid_page_does_not_reach_service(query):
    service, client = context()
    with client:
        assert client.get('/v2/favorites?' + query).status_code == 422
    service.list.assert_not_called()


@pytest.mark.parametrize('failure,status', [(FavoriteInvalid('input'), 422), (FavoriteNotFound('reading'), 404),
    (DataNotFound('owner'), 404), (OwnerDeleting('owner'), 409), (FavoriteConflict('key'), 409)])
def test_domain_errors_return_expected_status(failure, status):
    service, client = context()
    service.add.side_effect = failure
    with client:
        response = client.post('/v2/favorites', json={'reading_id': str(READING)}, headers={'X-Idempotency-Key': str(KEY)})
        assert response.status_code == status


def test_authentication_dependency_protects_all_favorite_operations():
    def no_session():
        raise HTTPException(401, 'session required')
    service, client = context(no_session)
    with client:
        assert client.get('/v2/favorites').status_code == 401
        assert client.post('/v2/favorites', json={'reading_id': str(READING)}, headers={'X-Idempotency-Key': str(KEY)}).status_code == 401
        assert client.delete('/v2/favorites/' + str(READING)).status_code == 401
    assert service.method_calls == []
