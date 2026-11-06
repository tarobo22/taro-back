"""Google Play verification. Network errors never become successful receipts."""
from datetime import datetime
from threading import Lock
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken

from api.domain.billing import BillingRejected, BillingUnavailable, StorePurchase


class FernetReceiptCipher:
    def __init__(self, key_supplier):self.key_supplier = key_supplier
    def _cipher(self):
        try:return Fernet(self.key_supplier() or '')
        except (ValueError, TypeError):raise BillingUnavailable('receipt_key_not_configured') from None
    def available(self):
        try:self._cipher();return True
        except BillingUnavailable:return False
    def seal(self, value):return self._cipher().encrypt(value.encode()).decode()
    def open(self, value):
        try:return self._cipher().decrypt(value.encode()).decode()
        except (InvalidToken, UnicodeError):raise BillingUnavailable('receipt_key_mismatch') from None


class GoogleApplicationCredentials:
    """Resolve ADC only at runtime; no credential value is logged or returned to clients."""
    def __init__(self):self.credentials = None;self.lock = Lock()
    def __call__(self):
        import google.auth
        from google.auth.transport.requests import Request
        try:
            with self.lock:
                if self.credentials is None:
                    self.credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/androidpublisher'])
                if not self.credentials.valid:self.credentials.refresh(Request())
                return self.credentials.token
        except Exception:raise BillingUnavailable('store_credentials_unavailable') from None


class GooglePlayVerifier:
    def __init__(self, client, package_name, token_supplier, *, enabled=False):
        self.client, self.package_name, self.token_supplier, self.enabled = client, package_name, token_supplier, enabled
    def available(self):return bool(self.enabled and self.package_name)
    def _request(self, method, suffix, **kwargs):
        if not self.available():raise BillingUnavailable('store_not_configured')
        try:
            response = self.client.request(method,
                'https://androidpublisher.googleapis.com/androidpublisher/v3/applications/' + quote(self.package_name, safe='') + suffix,
                headers={'Authorization': 'Bearer ' + self.token_supplier()}, timeout=10, **kwargs)
        except httpx.HTTPError:raise BillingUnavailable('store_unreachable') from None
        if response.status_code in (400, 404, 410):raise BillingRejected('receipt_invalid')
        if not response.is_success:raise BillingUnavailable('store_verification_unavailable')
        return response
    def verify(self, product, token):
        suffix = ('/purchases/productsv2/tokens/' if product.kind == 'inapp' else '/purchases/subscriptionsv2/tokens/') + quote(token, safe='')
        response = self._request('GET', suffix)
        try:
            data = response.json()
            if product.kind == 'inapp':
                items = data['productLineItem']
                if len(items) != 1 or items[0]['productId'] != product.product_id:raise BillingRejected('receipt_product_mismatch')
                offer = items[0].get('productOfferDetails', {})
                if offer.get('rentOfferDetails') is not None:raise BillingRejected('rental_not_supported')
                raw_state = data['purchaseStateContext']['purchaseState']
                state = {'PURCHASED': 'purchased', 'PENDING': 'pending', 'CANCELLED': 'revoked'}[raw_state]
                if state == 'purchased' and offer.get('refundableQuantity') == 0:state = 'revoked'
                if offer.get('consumptionState') == 'CONSUMPTION_STATE_CONSUMED':state = 'revoked'
                return StorePurchase(product.product_id, data.get('obfuscatedExternalAccountId'), state, None,
                    data['acknowledgementState'] == 'ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED')
            items = data['lineItems']
            if len(items) != 1 or items[0]['productId'] != product.product_id:raise BillingRejected('receipt_product_mismatch')
            raw_state = data['subscriptionState']
            if raw_state in ('SUBSCRIPTION_STATE_ACTIVE', 'SUBSCRIPTION_STATE_IN_GRACE_PERIOD', 'SUBSCRIPTION_STATE_CANCELED'):
                state = 'purchased'
            elif raw_state == 'SUBSCRIPTION_STATE_PENDING':state = 'pending'
            elif raw_state in ('SUBSCRIPTION_STATE_PAUSED', 'SUBSCRIPTION_STATE_ON_HOLD', 'SUBSCRIPTION_STATE_EXPIRED',
                               'SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED'):state = 'revoked'
            else:raise BillingRejected('unknown_subscription_state')
            expires = datetime.fromisoformat(items[0]['expiryTime'].replace('Z', '+00:00')) if items[0].get('expiryTime') else None
            return StorePurchase(product.product_id, data.get('externalAccountIdentifiers', {}).get('obfuscatedExternalAccountId'),
                state, expires, data['acknowledgementState'] == 'ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED', data.get('linkedPurchaseToken'))
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise BillingRejected('invalid_store_response') from None
    def acknowledge(self, product, token):
        resource = 'products' if product.kind == 'inapp' else 'subscriptions'
        suffix = '/purchases/' + resource + '/' + quote(product.product_id, safe='') + '/tokens/' + quote(token, safe='') + ':acknowledge'
        try:self._request('POST', suffix, json={})
        except BillingRejected:raise BillingUnavailable('store_acknowledgement_retry_required') from None
