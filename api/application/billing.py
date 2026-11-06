from datetime import datetime, timedelta, timezone
import hashlib

from api.domain.billing import BillingInvalid, BillingRejected, BillingUnavailable, StorePurchase, entitlement_state


class BillingService:
    def __init__(self, uow_factory, products, verifier, cipher, *, clock=None, owner_kind=None,
                 max_receipts=100, reverify_seconds=300, package_name='com.yeoun.app'):
        self.uow_factory, self.products = uow_factory, {p.product_id: p for p in products}
        if len(self.products) != len(products):raise BillingInvalid('duplicate_product')
        self.verifier, self.cipher = verifier, cipher
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.owner_kind = owner_kind or (lambda c,o:'guest')
        self.max_receipts, self.reverify_seconds, self.package_name = max_receipts, reverify_seconds, package_name

    @staticmethod
    def digest(value):return hashlib.sha256(value.encode()).hexdigest()

    @classmethod
    def account_tag(cls, owner):return cls.digest('yeoun:billing:' + str(owner))

    def available(self):return bool(self.products and self.verifier.available() and self.cipher.available())

    def catalog(self, owner):
        with self.uow_factory() as uow:
            uow.receipts.lock(owner);uow.receipts.active(owner)
            member = self.owner_kind(uow.connection, owner) == 'member'
        reason = None if self.available() else ('catalog_not_configured' if not self.products else 'store_not_configured')
        return {'store': 'google_play', 'available': self.available(), 'reason': reason,
                'can_purchase': member and self.available(), 'account_id': self.account_tag(owner),
                'package_name': self.package_name,
                'products': [{'product_id': p.product_id, 'kind': p.kind, 'entitlement_key': p.entitlement_key}
                             for p in self.products.values()]}

    def _snapshot(self, repository, owner):
        items = {}
        for row in repository.list(owner):
            if row['state'] != 'active' or (row['expires_at'] is not None and row['expires_at'] <= self.clock()):continue
            key = row['entitlement_key'];previous = items.get(key)
            if previous is None or (previous['expires_at'] is not None and
                    (row['expires_at'] is None or row['expires_at'] > previous['expires_at'])):
                items[key] = {field: row[field] for field in ('product_id', 'expires_at', 'verified_at')}
                items[key]['key'] = key
        return sorted(items.values(), key=lambda x:x['key'])

    def verify(self, owner, product_id, token):
        product = self.products.get(product_id)
        if product is None:raise BillingInvalid('unknown_product')
        if not isinstance(token, str) or not 1 <= len(token) <= 4096 or any(ord(c)<33 for c in token):
            raise BillingInvalid('invalid_receipt')
        if not self.available():raise BillingUnavailable('store_not_configured')
        token_hash = self.digest(token)
        with self.uow_factory() as uow:
            uow.receipts.lock(owner, token_hash);uow.receipts.active(owner)
            if self.owner_kind(uow.connection, owner) != 'member':raise BillingRejected('account_required')
            previous = uow.receipts.get(token_hash)
            if previous and previous['owner_id'] != owner:raise BillingRejected('receipt_owner_mismatch')
            if previous is None and uow.receipts.count(owner) >= self.max_receipts:raise BillingRejected('receipt_limit')
            try:
                purchase = self.verifier.verify(product, token)
            except BillingRejected as error:
                if previous and str(error) == 'receipt_invalid':
                    purchase = StorePurchase(product.product_id, self.account_tag(owner), 'revoked', None, True)
                else:raise
            if purchase.product_id != product.product_id or purchase.account_id != self.account_tag(owner):
                raise BillingRejected('receipt_account_or_product_mismatch')
            if product.kind == 'subs' and purchase.state == 'purchased' and purchase.expires_at is None:
                raise BillingRejected('subscription_expiry_missing')
            state = entitlement_state(purchase, self.clock())
            if purchase.linked_token:
                linked_hash = self.digest(purchase.linked_token)
                linked = uow.receipts.get(linked_hash)
                if linked and linked['owner_id'] != owner:raise BillingRejected('linked_receipt_owner_mismatch')
                uow.receipts.revoke_linked(owner, linked_hash)
            values = {'token_hash': token_hash, 'owner_id': owner, 'product_id': product.product_id,
                      'product_kind': product.kind, 'entitlement_key': product.entitlement_key,
                      'token_ciphertext': self.cipher.seal(token), 'state': state,
                      'expires_at': purchase.expires_at, 'acknowledged': purchase.acknowledged,
                      'verified_at': self.clock()}
            uow.receipts.save(values);uow.commit()
        acknowledged = purchase.acknowledged
        if state == 'active' and not acknowledged:
            try:
                self.verifier.acknowledge(product, token)
            except BillingUnavailable:
                pass  # Durable grant remains; foreground restore/maintenance retries acknowledgement.
            else:
                with self.uow_factory() as uow:
                    uow.receipts.lock(owner, token_hash);uow.receipts.active(owner)
                    uow.receipts.acknowledge(owner, token_hash);uow.commit()
                acknowledged = True
        with self.uow_factory() as uow:
            uow.receipts.lock(owner);uow.receipts.active(owner)
            entitlements = self._snapshot(uow.receipts, owner)
        return {'state': state, 'acknowledged': acknowledged, 'entitlements': entitlements}

    def entitlements(self, owner):
        with self.uow_factory() as uow:
            uow.receipts.lock(owner);uow.receipts.active(owner)
            due = [r for r in uow.receipts.list(owner) if
                   r['verified_at'] + timedelta(seconds=self.reverify_seconds) <= self.clock()]
        for row in due:
            self.verify(owner, row['product_id'], self.cipher.open(row['token_ciphertext']))
        with self.uow_factory() as uow:
            uow.receipts.lock(owner);uow.receipts.active(owner)
            return {'items': self._snapshot(uow.receipts, owner), 'checked_at': self.clock()}

    def reconcile(self, limit=10):
        if not self.available():return 0
        with self.uow_factory() as uow:
            due = uow.receipts.due(self.clock() - timedelta(seconds=self.reverify_seconds), limit)
        completed = 0
        for row in due:
            try:self.verify(row['owner_id'], row['product_id'], self.cipher.open(row['token_ciphertext']))
            except (BillingInvalid, BillingRejected, BillingUnavailable):continue
            completed += 1
        return completed

    def deck_access(self, connection, owner, deck_id):
        """Use the reading transaction; never acquire another pool connection or call a store here."""
        key = 'deck:' + str(deck_id)
        if not any(p.entitlement_key == key for p in self.products.values()):return True
        fresh_after = self.clock() - timedelta(seconds=self.reverify_seconds)
        rows = self.uow_factory().repository_for(connection).list(owner)
        return any(r['entitlement_key'] == key and r['state'] == 'active' and r['verified_at'] >= fresh_after and
                   (r['expires_at'] is None or r['expires_at'] > self.clock()) for r in rows)

    def export_owner(self, connection, owner):
        rows = self.uow_factory().repository_for(connection).list(owner)
        fields = ('product_id', 'product_kind', 'entitlement_key', 'state', 'expires_at', 'acknowledged', 'verified_at')
        return {'store_purchases': [{k:r[k] for k in fields} for r in rows]}

    def erase_owner(self, connection, owner):self.uow_factory().repository_for(connection).erase(owner)
