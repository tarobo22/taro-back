"""Verify the store boundary against real PostgreSQL advisory locks."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from api.adapters.billing_postgres import PostgresBillingUnitOfWork
from api.application.billing import BillingService
from api.domain.billing import Product, StorePurchase
from api.domain.data_privacy import OwnerDeleting
from api.tests.integration.test_data_privacy_profile_catalog import data_context

pytestmark = pytest.mark.integration


class IsolatedCipher:
    def available(self): return True
    def seal(self, value): return 'test-cipher:' + value
    def open(self, value): return value.removeprefix('test-cipher:')


def test_slow_store_releases_the_actual_pg_owner_lock_and_rechecks_deletion(data_context):
    _, _, data, owners, pool = data_context
    owner = owners[0]
    data.get_me(owner)
    entered, release = Event(), Event()

    class SlowStore:
        def available(self): return True
        def verify(self, product, token):
            entered.set()
            assert release.wait(5), 'test must release the synthetic store response'
            return StorePurchase(product.product_id, service.account_tag(owner), 'purchased', None, True)
        def acknowledge(self, *args): raise AssertionError('already acknowledged')

    service = BillingService(lambda: PostgresBillingUnitOfWork(pool),
        [Product('test.slow.store', 'inapp', 'deck:slow')], SlowStore(), IsolatedCipher(),
        owner_kind=lambda c, o: 'member')
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.verify, owner, 'test.slow.store', 'synthetic-slow-store-receipt')
        try:
            assert entered.wait(5)
            with pool.connection() as connection:
                lock = connection.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0)) AS acquired',
                    (f'yeoun:owner:{owner}',)).fetchone()
                assert lock['acquired'] is True, 'store latency retained the actual PostgreSQL owner lock'
                connection.execute("UPDATE data_privacy_profiles SET state='deleting' WHERE owner_id=%s", (owner,))
        finally:
            release.set()
        with pytest.raises(OwnerDeleting): future.result(timeout=5)
    with pool.connection() as connection:
        assert connection.execute('SELECT count(*) AS n FROM billing_receipts WHERE owner_id=%s', (owner,)).fetchone()['n'] == 0
