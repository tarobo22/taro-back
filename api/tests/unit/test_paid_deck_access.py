from datetime import timedelta
from uuid import uuid4
import pytest
from api.tests.unit.test_billing import context, NOW
from api.domain.billing import Product
from api.application.reading_v2 import ReadingV2Service
from api.domain.reading_v2 import DeckAccessDenied


def test_paid_deck_requires_a_fresh_active_owned_entitlement_and_free_deck_stays_free():
    owner, billing, verifier, rows, _ = context()
    paid, free = uuid4(), uuid4()
    billing.products['test.deck'] = Product('test.deck', 'inapp', 'deck:' + str(paid))
    class Repo:
        def list(self, who):return [row for row in rows.values() if row['owner_id'] == who]
    original_factory = billing.uow_factory
    class UowFactory:
        def __call__(self):
            uow=original_factory();uow.repository_for=lambda c:Repo();return uow
    billing.uow_factory=UowFactory()
    assert billing.deck_access(object(), owner, free)
    assert not billing.deck_access(object(), owner, paid)
    billing.verify(owner, 'test.deck', 'paid-deck-receipt')
    assert billing.deck_access(object(), owner, paid)
    assert not billing.deck_access(object(), uuid4(), paid)
    next(iter(rows.values()))['verified_at'] = NOW - timedelta(seconds=301)
    assert not billing.deck_access(object(), owner, paid)


def test_reading_rejects_paid_deck_before_generating_or_persisting_any_cards():
    owner, paid = uuid4(), uuid4();events=[]
    class Repo:
        transaction=object()
        def lock_owner(self, value):events.append('lock')
        def owner_available(self, value, guard):return True
        def command(self, *args):return None
        def discard_command(self, *args):pass
    class Uow:
        readings=Repo()
        def __enter__(self):return self
        def __exit__(self, *args):pass
    class Catalog:
        def get_deck(self, value):raise AssertionError('denied before catalog or random draw')
    service=ReadingV2Service(Uow,Catalog(),deck_access=lambda tx,who,deck:False)
    with pytest.raises(DeckAccessDenied):service.create(owner,uuid4(),{'kind':'three_card','topic':'self','deck_version_id':str(paid)})
    assert events == ['lock']
