import hashlib
import hmac
import json

import httpx

from cloud.billing import coinpay as cp
from cloud.billing.config import CoinPayConfig


def test_signature_matches_coinpay_reference():
    # The portal signs `${ts}.${JSON.stringify(payload)}` and sends that same
    # string as the body, so signing the raw bytes is the same computation.
    body = json.dumps({"event": "payment.confirmed", "payment_id": "p1", "timestamp": "x"}, separators=(",", ":")).encode()
    ts = 1_700_000_000
    header = cp.sign_webhook(body, "sec", ts)
    expected = hmac.new(b"sec", f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    assert header == f"t={ts},v1={expected}"
    assert cp.verify_webhook(body, header, "sec", now=ts + 10)
    assert cp.verify_webhook(body, " t=%d , v1=%s " % (ts, expected), "sec", now=ts + 10)


def test_verify_rejects_stale_tampered_and_missing():
    body = b'{"a":1}'
    ts = 1_700_000_000
    header = cp.sign_webhook(body, "sec", ts)
    assert not cp.verify_webhook(body, header, "sec", now=ts + 301)
    assert not cp.verify_webhook(body, header, "sec", now=ts - 301)
    assert not cp.verify_webhook(b'{"a":2}', header, "sec", now=ts)
    assert not cp.verify_webhook(body, header, "wrong", now=ts)
    assert not cp.verify_webhook(body, None, "sec", now=ts)
    assert not cp.verify_webhook(body, "v1=abc", "sec", now=ts)
    assert not cp.verify_webhook(body, header, "", now=ts)


def test_is_settled():
    assert cp.is_settled("payment.confirmed", "pending")
    assert cp.is_settled(None, "forwarded")
    assert cp.is_settled(None, "Confirmed")
    assert not cp.is_settled("payment.expired", "expired")
    assert not cp.is_settled(None, "pending")
    assert not cp.is_settled(None, None)


def _cfg(**over):
    base = dict(coinpay_base_url="https://coinpay.test", coinpay_api_key="cp_test_x", coinpay_business_id="biz")
    base.update(over)
    return CoinPayConfig(**base)


def test_create_payment_sends_merchant_contract():
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "payment": {"id": "pay_1", "status": "pending"}, "usage": {}})

    client = cp.CoinPayClient(_cfg(), transport=httpx.MockTransport(handler))
    out = client.create_payment(amount_usd=12.0, blockchain="USDC_BASE", description="d", metadata={"k": "v"},
                                redirect_url="http://web.test/ok", idempotency_key="idem-1")
    assert out.id == "pay_1"
    assert out.pay_url == "https://coinpay.test/pay/pay_1"
    assert seen["url"] == "https://coinpay.test/api/payments/create"
    assert seen["headers"]["authorization"] == "Bearer cp_test_x"
    assert seen["headers"]["idempotency-key"] == "idem-1"
    assert seen["body"] == {"business_id": "biz", "amount": 12.0, "currency": "USD", "blockchain": "USDC_BASE",
                            "payment_method": "crypto", "description": "d", "metadata": {"k": "v"},
                            "redirect_url": "http://web.test/ok"}


def test_create_payment_errors():
    client = cp.CoinPayClient(_cfg(), transport=httpx.MockTransport(lambda r: httpx.Response(400, json={"success": False, "error": "nope"})))
    try:
        client.create_payment(amount_usd=1, blockchain="BTC", description="", metadata={}, redirect_url=None, idempotency_key="k")
    except cp.CoinPayError as exc:
        assert "400" in str(exc) and "nope" in str(exc)
    else:
        raise AssertionError("expected CoinPayError")
    client = cp.CoinPayClient(_cfg(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True})))
    try:
        client.create_payment(amount_usd=1, blockchain="BTC", description="", metadata={}, redirect_url=None, idempotency_key="k")
    except cp.CoinPayError as exc:
        assert "no payment id" in str(exc)


def test_get_payment_public():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, json={"success": False})
        return httpx.Response(200, json={"success": True, "payment": {"id": "p", "status": "confirmed"}})

    client = cp.CoinPayClient(_cfg(), transport=httpx.MockTransport(handler))
    assert client.get_payment("p") == {"id": "p", "status": "confirmed"}
    assert client.get_payment("missing") is None
