from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from api.domain.billing import BillingInvalid, BillingRejected, Product, StorePurchase, entitlement_state
from api.application.billing import BillingService

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
PRODUCT = Product('test.deck', 'inapp', 'deck:test')


def test_store_states_never_grant_pending_revoked_expired_or_paused_purchases():
    assert entitlement_state(StorePurchase('test.deck', 'account', 'purchased', None, False), NOW) == 'active'
    assert entitlement_state(StorePurchase('test.deck', 'account', 'pending', None, False), NOW) == 'pending'
    assert entitlement_state(StorePurchase('test.deck', 'account', 'revoked', None, True), NOW) == 'revoked'
    assert entitlement_state(StorePurchase('test.deck', 'account', 'purchased', NOW, True), NOW) == 'expired'
    # A canceled renewal retains only the already-paid interval, never an indefinite grant.
    assert entitlement_state(StorePurchase('test.deck', 'account', 'purchased', NOW + timedelta(days=1), True), NOW) == 'active'
    with pytest.raises(BillingInvalid):Product('test.deck', 'consumable', 'deck:test')


class Cipher:
    def available(self):return True
    def seal(self, value):return 'encrypted:' + value
    def open(self, value):return value.removeprefix('encrypted:')


class Verifier:
    def __init__(self):self.state = 'purchased';self.account = None;self.ack = [];self.expires = None
    def available(self):return True
    def verify(self, product, token):return StorePurchase(product.product_id, self.account, self.state, self.expires, False)
    def acknowledge(self, product, token):self.ack.append(token)


def context():
    owner = uuid4();rows = {};commits = []
    class Repo:
        def lock(self, current_owner, token_hash=None):assert current_owner == owner
        def active(self, current_owner):assert current_owner == owner
        def get(self, token_hash):return rows.get(token_hash)
        def count(self, current_owner):return len(rows)
        def save(self, values):rows[values['token_hash']] = values;return values
        def acknowledge(self, current_owner, token_hash):rows[token_hash]['acknowledged'] = True
        def list(self, current_owner):return [v for v in rows.values() if v['owner_id'] == current_owner]
        def revoke_linked(self, current_owner, token_hash):
            if token_hash in rows:rows[token_hash]['state'] = 'revoked'
    class Uow:
        receipts = Repo();connection = object()
        def __enter__(self):return self
        def __exit__(self, *args):pass
        def commit(self):commits.append(True)
    verifier = Verifier()
    service = BillingService(Uow, [PRODUCT], verifier, Cipher(), clock=lambda: NOW, owner_kind=lambda c,o:'member')
    verifier.account = service.account_tag(owner)
    return owner, service, verifier, rows, commits


def test_verified_replay_grants_one_receipt_and_refund_revokes_the_same_entitlement():
    owner, service, verifier, rows, commits = context()
    first = service.verify(owner, 'test.deck', 'valid-store-receipt')
    second = service.verify(owner, 'test.deck', 'valid-store-receipt')
    assert first['entitlements'] == second['entitlements']
    assert len(rows) == 1 and first['entitlements'][0]['key'] == 'deck:test'
    assert all('purchase_token' not in item for item in first['entitlements'])
    assert next(iter(rows.values()))['token_ciphertext'] == 'encrypted:valid-store-receipt'
    verifier.state = 'revoked'
    assert service.verify(owner, 'test.deck', 'valid-store-receipt')['entitlements'] == []
    assert next(iter(rows.values()))['state'] == 'revoked'


def test_pending_never_acknowledges_and_owner_mismatch_or_unknown_product_never_saves():
    owner, service, verifier, rows, _ = context()
    verifier.state = 'pending'
    assert service.verify(owner, 'test.deck', 'pending-store-receipt')['state'] == 'pending'
    assert verifier.ack == [] and service.entitlements(owner)['items'] == []
    verifier.account = 'another-account'
    with pytest.raises(BillingRejected):service.verify(owner, 'test.deck', 'foreign-store-receipt')
    with pytest.raises(BillingInvalid):service.verify(owner, 'unknown.sku', 'receipt')
    assert len(rows) == 1


def test_unknown_receipt_cannot_move_between_owners_and_guest_must_link_before_purchase():
    owner, service, verifier, rows, _ = context()
    service.verify(owner, 'test.deck', 'owned-store-receipt')
    next(iter(rows.values()))['owner_id'] = uuid4()
    with pytest.raises(BillingRejected):service.verify(owner, 'test.deck', 'owned-store-receipt')
    service.owner_kind = lambda c,o:'guest'
    with pytest.raises(BillingRejected):service.verify(owner, 'test.deck', 'new-store-receipt')


def test_pending_or_cancelled_subscription_replacement_keeps_the_previous_paid_interval():
    from dataclasses import replace
    owner, service, verifier, rows, _ = context()
    service.products = {'test.month': Product('test.month', 'subs', 'feature:test')}
    verifier.expires = NOW + timedelta(days=10)
    original = verifier.verify
    verifier.verify = lambda product,token:replace(original(product,token), linked_token='old-subscription' if token=='replacement' else None)
    service.verify(owner, 'test.month', 'old-subscription')
    for state in ('pending', 'revoked'):
        verifier.state = state
        result = service.verify(owner, 'test.month', 'replacement')
        assert result['entitlements'][0]['key'] == 'feature:test'
        assert rows[service.digest('old-subscription')]['state'] == 'active'
    verifier.state = 'purchased'
    assert service.verify(owner, 'test.month', 'replacement')['state'] == 'active'
    assert rows[service.digest('old-subscription')]['state'] == 'revoked'
