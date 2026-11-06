"""Store-independent purchase rules. Prices and offers remain owned by the store."""
from dataclasses import dataclass
from datetime import datetime
import re


class BillingInvalid(ValueError):pass
class BillingRejected(Exception):pass
class BillingUnavailable(Exception):pass


@dataclass(frozen=True)
class Product:
    product_id: str
    kind: str
    entitlement_key: str

    def __post_init__(self):
        if (self.kind not in ('inapp', 'subs') or not re.fullmatch(r'[A-Za-z0-9._-]{1,150}', self.product_id)
                or not re.fullmatch(r'[A-Za-z0-9:._-]{1,150}', self.entitlement_key)):
            raise BillingInvalid('invalid_product')


@dataclass(frozen=True)
class StorePurchase:
    product_id: str
    account_id: str | None
    state: str
    expires_at: datetime | None
    acknowledged: bool
    linked_token: str | None = None


def entitlement_state(purchase, now):
    if purchase.state not in ('purchased', 'pending', 'revoked'):
        raise BillingInvalid('invalid_purchase_state')
    if purchase.expires_at is not None:
        if purchase.expires_at.tzinfo is None:raise BillingInvalid('invalid_purchase_expiry')
        if purchase.state == 'purchased' and purchase.expires_at <= now:return 'expired'
    return {'purchased': 'active', 'pending': 'pending', 'revoked': 'revoked'}[purchase.state]
