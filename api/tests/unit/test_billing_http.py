from datetime import datetime, timezone
from uuid import uuid4
from fastapi import FastAPI
from fastapi.testclient import TestClient
from api.adapters.http.billing import billing_router
from api.domain.billing import BillingRejected, BillingUnavailable


def test_billing_http_rejects_client_prices_and_hides_private_purchase_tokens():
    owner = uuid4();calls = []
    class Service:
        def catalog(self, current):
            assert current == owner
            return {'store': 'google_play', 'available': False, 'reason': 'catalog_not_configured',
                    'can_purchase': False, 'account_id': 'a'*64, 'package_name': 'com.test.app', 'products': []}
        def verify(self, current, product, token):
            assert current == owner;calls.append((product, token))
            if token == 'outage':raise BillingUnavailable()
            raise BillingRejected()
        def entitlements(self, current):return {'items': [], 'checked_at': datetime.now(timezone.utc)}
    app = FastAPI();app.include_router(billing_router(Service(), lambda:owner))
    with TestClient(app) as client:
        response = client.get('/v2/billing/catalog')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        assert response.json()['products'] == [] and response.json()['available'] is False
        assert client.post('/v2/billing/purchases/verify', json={'product_id': 'test.deck', 'purchase_token': 'token', 'price': 0}).status_code == 422
        denied = client.post('/v2/billing/purchases/verify', json={'product_id': 'test.deck', 'purchase_token': 'token'})
        assert denied.status_code == 403 and 'token' not in denied.text
        assert client.post('/v2/billing/purchases/verify', json={'product_id': 'test.deck', 'purchase_token': 'outage'}).status_code == 503
        assert client.get('/v2/billing/entitlements').json()['items'] == []
    assert calls == [('test.deck', 'token'), ('test.deck', 'outage')]
