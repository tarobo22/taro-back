"""Store latency must not retain owner locks or overwrite a newer receipt."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from api.application.billing import BillingService
from api.domain.billing import BillingRejected, BillingUnavailable, Product, StorePurchase
from api.domain.data_privacy import OwnerDeleting

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
PRODUCT = Product('test.network.deck', 'inapp', 'deck:network')
TOKEN = 'synthetic-billing-boundary-receipt'


class Cipher:
    def available(self): return True
    def seal(self, value): return 'test-cipher:' + value
    def open(self, value): return value.removeprefix('test-cipher:')


class Scenario:
    def __init__(self):
        self.owner, self.depth, self.rows = uuid4(), 0, {}
        self.active, self.member, self.calls, self.acknowledged = True, True, 0, []
        self.on_verify = lambda: None
        self.state = 'purchased'
        scenario = self

        class Repository:
            def lock(self, owner, token_hash=None): assert scenario.depth == 1
            def active(self, owner):
                if not scenario.active: raise OwnerDeleting('owner_deleting')
            def get(self, token_hash): return deepcopy(scenario.rows.get(token_hash))
            def count(self, owner): return sum(r['owner_id'] == owner for r in scenario.rows.values())
            def save(self, values):
                previous = scenario.rows.get(values['token_hash'])
                scenario.rows[values['token_hash']] = {**values, 'verification_version': 1 + (previous or {}).get('verification_version', 0)}
            def list(self, owner): return [deepcopy(r) for r in scenario.rows.values() if r['owner_id'] == owner]
            def acknowledge(self, owner, token_hash): scenario.rows[token_hash]['acknowledged'] = True
            def revoke_linked(self, owner, token_hash):
                if token_hash in scenario.rows:
                    scenario.rows[token_hash]['state'] = 'revoked'
                    scenario.rows[token_hash]['verification_version'] += 1

        class UnitOfWork:
            connection = object()
            def __enter__(self):
                scenario.depth += 1
                self.receipts = Repository()
                return self
            def __exit__(self, *args): scenario.depth -= 1
            def commit(self): pass

        class Verifier:
            def available(self): return True
            def verify(self, product, token):
                assert scenario.depth == 0, 'external store verification retained a database transaction'
                scenario.calls += 1
                result = StorePurchase(product.product_id, scenario.service.account_tag(scenario.owner), scenario.state, None, False)
                scenario.on_verify()
                return result
            def acknowledge(self, product, token):
                assert scenario.depth == 0
                assert scenario.rows[scenario.service.digest(token)]['state'] == 'active'
                scenario.acknowledged.append(token)

        self.service = BillingService(UnitOfWork, [PRODUCT], Verifier(), Cipher(), clock=lambda: NOW,
            owner_kind=lambda c, o: 'member' if self.member else 'guest')

    @property
    def row(self): return self.rows[self.service.digest(TOKEN)]


def test_external_verify_and_acknowledgement_run_without_a_database_transaction():
    scenario = Scenario()
    result = scenario.service.verify(scenario.owner, PRODUCT.product_id, TOKEN)
    assert result['state'] == 'active'
    assert scenario.calls == 1 and scenario.acknowledged == [TOKEN]
    assert scenario.row['verification_version'] == 1 and scenario.row['acknowledged'] is True


def test_owner_deleted_during_store_latency_never_receives_a_late_grant():
    scenario = Scenario()
    scenario.on_verify = lambda: setattr(scenario, 'active', False)
    with pytest.raises(OwnerDeleting): scenario.service.verify(scenario.owner, PRODUCT.product_id, TOKEN)
    assert scenario.rows == {} and scenario.acknowledged == []


def test_member_state_is_rechecked_after_external_io_before_saving():
    scenario = Scenario()
    scenario.on_verify = lambda: setattr(scenario, 'member', False)
    with pytest.raises(BillingRejected, match='account_required'):
        scenario.service.verify(scenario.owner, PRODUCT.product_id, TOKEN)
    assert scenario.rows == {} and scenario.acknowledged == []


def test_receipt_changed_during_store_io_is_reverified_instead_of_overwriting_refund():
    scenario = Scenario()
    scenario.service.verify(scenario.owner, PRODUCT.product_id, TOKEN)

    def refund_during_io():
        scenario.row['state'] = 'revoked'
        scenario.row['verification_version'] += 1
        scenario.state = 'revoked'
        scenario.on_verify = lambda: None

    scenario.on_verify = refund_during_io
    result = scenario.service.verify(scenario.owner, PRODUCT.product_id, TOKEN)
    assert scenario.calls == 3
    assert result['state'] == 'revoked' and result['entitlements'] == []
    assert scenario.row['state'] == 'revoked'


def test_repeated_concurrent_changes_are_bounded_and_never_commit_a_stale_success():
    scenario = Scenario()
    scenario.service.verify(scenario.owner, PRODUCT.product_id, TOKEN)
    initial_calls = scenario.calls

    def keep_changing():
        scenario.row['state'] = 'revoked'
        scenario.row['verification_version'] += 1

    scenario.on_verify = keep_changing
    with pytest.raises(BillingUnavailable, match='receipt_changed_during_verification'):
        scenario.service.verify(scenario.owner, PRODUCT.product_id, TOKEN)
    assert scenario.calls - initial_calls == 3
    assert scenario.row['state'] == 'revoked'
