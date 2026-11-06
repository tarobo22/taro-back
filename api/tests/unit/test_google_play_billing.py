from datetime import datetime, timezone

from cryptography.fernet import Fernet
import httpx
import pytest

from api.adapters.google_play_billing import FernetReceiptCipher, GooglePlayVerifier
from api.domain.billing import BillingRejected, BillingUnavailable, Product


def client_for(payload, status=200):
    calls = []
    def handle(request):calls.append(request);return httpx.Response(status, json=payload)
    return httpx.Client(transport=httpx.MockTransport(handle)), calls


def test_product_v2_requires_matching_single_sku_and_maps_pending_refund_and_ack_without_client_price():
    body = {'productLineItem': [{'productId': 'test.deck', 'productOfferDetails': {'quantity': 1, 'refundableQuantity': 1}}],
            'purchaseStateContext': {'purchaseState': 'PENDING'}, 'obfuscatedExternalAccountId': 'account',
            'acknowledgementState': 'ACKNOWLEDGEMENT_STATE_PENDING'}
    client, calls = client_for(body)
    verifier = GooglePlayVerifier(client, 'com.test.app', lambda:'generated-fixture-token', enabled=True)
    product = Product('test.deck', 'inapp', 'deck:test')
    assert verifier.verify(product, 'receipt/escaped').state == 'pending'
    assert '%2F' in str(calls[-1].url)
    body['purchaseStateContext']['purchaseState'] = 'PURCHASED'
    assert verifier.verify(product, 'receipt').state == 'purchased'
    body['productLineItem'][0]['productOfferDetails']['refundableQuantity'] = 0
    assert verifier.verify(product, 'receipt').state == 'revoked'
    body['productLineItem'][0]['productId'] = 'another.sku'
    with pytest.raises(BillingRejected):verifier.verify(product, 'receipt')
    verifier.acknowledge(product, 'receipt')
    assert calls[-1].method == 'POST' and calls[-1].url.path.endswith(':acknowledge')
    assert calls[-1].content == b'{}'


def test_subscription_cancel_retains_paid_interval_but_hold_revokes_and_unknown_states_fail_closed():
    body = {'lineItems': [{'productId': 'test.month', 'expiryTime': '2026-12-06T00:00:00Z'}],
            'subscriptionState': 'SUBSCRIPTION_STATE_CANCELED', 'externalAccountIdentifiers': {'obfuscatedExternalAccountId': 'account'},
            'acknowledgementState': 'ACKNOWLEDGEMENT_STATE_ACKNOWLEDGED'}
    client, _ = client_for(body)
    verifier = GooglePlayVerifier(client, 'com.test.app', lambda:'generated-fixture-token', enabled=True)
    product = Product('test.month', 'subs', 'feature:test')
    purchase = verifier.verify(product, 'receipt')
    assert purchase.state == 'purchased' and purchase.expires_at == datetime(2026, 12, 6, tzinfo=timezone.utc)
    body['subscriptionState'] = 'SUBSCRIPTION_STATE_ON_HOLD'
    assert verifier.verify(product, 'receipt').state == 'revoked'
    body['subscriptionState'] = 'NEW_UNRECOGNIZED_STATE'
    with pytest.raises(BillingRejected):verifier.verify(product, 'receipt')


def test_store_outage_or_disabled_configuration_never_grants_a_purchase():
    client, _ = client_for({}, status=503)
    verifier = GooglePlayVerifier(client, 'com.test.app', lambda:'generated-fixture-token', enabled=True)
    with pytest.raises(BillingUnavailable):verifier.verify(Product('test.deck', 'inapp', 'deck:test'), 'receipt')
    verifier.enabled = False
    with pytest.raises(BillingUnavailable):verifier.verify(Product('test.deck', 'inapp', 'deck:test'), 'receipt')


def test_receipts_are_encrypted_with_runtime_key_and_wrong_key_is_rejected():
    cipher = FernetReceiptCipher(lambda:Fernet.generate_key())
    assert cipher.available()
    key = Fernet.generate_key();cipher = FernetReceiptCipher(lambda:key)
    sealed = cipher.seal('private-generated-fixture')
    assert 'private-generated-fixture' not in sealed and cipher.open(sealed) == 'private-generated-fixture'
    with pytest.raises(BillingUnavailable):FernetReceiptCipher(lambda:Fernet.generate_key()).open(sealed)
    assert not FernetReceiptCipher(lambda:None).available()
