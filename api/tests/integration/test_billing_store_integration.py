from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
import pytest

from api.adapters.billing_postgres import PostgresBillingUnitOfWork
from api.adapters.google_play_billing import FernetReceiptCipher
from api.application.billing import BillingService
from api.domain.billing import BillingRejected, Product, StorePurchase
from api.tests.integration.test_data_privacy_profile_catalog import data_context

pytestmark = pytest.mark.integration


def test_concurrent_receipt_verification_is_single_owned_and_export_and_erasure_exclude_tokens(data_context):
    _, _, _, owners, pool = data_context
    now = datetime.now(timezone.utc);key = Fernet.generate_key()
    class Verifier:
        state = 'purchased'
        def available(self):return True
        def verify(self, product, token):return StorePurchase(product.product_id, service.account_tag(owners[0]), self.state, None, True)
        def acknowledge(self, product, token):raise AssertionError('already acknowledged')
    verifier = Verifier()
    service = BillingService(lambda:PostgresBillingUnitOfWork(pool), [Product('test.deck', 'inapp', 'deck:test')],
        verifier, FernetReceiptCipher(lambda:key), owner_kind=lambda c,o:'member')
    token = 'isolated-concurrent-receipt'
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _:service.verify(owners[0], 'test.deck', token), range(2)))
    assert all(r['state'] == 'active' and len(r['entitlements']) == 1 for r in results)
    with pool.connection() as connection:
        rows = connection.execute('SELECT * FROM billing_receipts WHERE owner_id=%s', (owners[0],)).fetchall()
        assert len(rows) == 1 and token not in rows[0]['token_ciphertext']
        exported = service.export_owner(connection, owners[0])
        assert len(exported['store_purchases']) == 1 and token not in str(exported)
        assert 'token_hash' not in str(exported) and 'token_ciphertext' not in str(exported)
    with pytest.raises(BillingRejected):service.verify(owners[1], 'test.deck', token)
    verifier.state = 'revoked'
    service.verify(owners[0], 'test.deck', token)
    assert service.entitlements(owners[0])['items'] == []
    with pool.connection() as connection:
        service.erase_owner(connection, owners[0])
    assert service.entitlements(owners[0])['items'] == []


def test_expired_verification_is_refreshed_before_returning_entitlements(data_context):
    _, _, _, owners, pool = data_context
    now = datetime.now(timezone.utc);key = Fernet.generate_key();calls = []
    class Verifier:
        def available(self):return True
        def verify(self, product, token):
            calls.append(token)
            return StorePurchase(product.product_id, service.account_tag(owners[0]), 'purchased' if len(calls)==1 else 'revoked', None, True)
        def acknowledge(self, product, token):raise AssertionError('already acknowledged')
    service = BillingService(lambda:PostgresBillingUnitOfWork(pool), [Product('test.deck', 'inapp', 'deck:test')], Verifier(),
        FernetReceiptCipher(lambda:key), owner_kind=lambda c,o:'member', clock=lambda:now, reverify_seconds=300)
    service.verify(owners[0], 'test.deck', 'isolated-refresh-receipt')
    now += timedelta(seconds=301)
    assert service.entitlements(owners[0])['items'] == [] and len(calls) == 2
