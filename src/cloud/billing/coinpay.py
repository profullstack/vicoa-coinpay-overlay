"""The CoinPay HTTP client and webhook verification. No database access."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import CoinPayConfig

# Payment statuses that mean money arrived. ``confirmed`` is set by the chain
# monitor once the deposit has enough confirmations; ``forwarded`` once the
# funds moved on to the merchant wallet. Anything else grants nothing.
SETTLED_STATUSES = frozenset({"confirmed", "forwarded", "completed", "paid"})
SETTLED_EVENTS = frozenset({"payment.confirmed", "payment.forwarded"})
TERMINAL_FAILURES = frozenset({"expired", "failed", "cancelled", "canceled"})


class CoinPayError(RuntimeError):
    pass


@dataclass(frozen=True)
class CreatedPayment:
    id: str
    pay_url: str
    raw: dict[str, Any]


class CoinPayClient:
    def __init__(self, cfg: CoinPayConfig, transport: httpx.BaseTransport | None = None):
        self.cfg = cfg
        self._client = httpx.Client(
            base_url=cfg.coinpay_base_url.rstrip("/"),
            timeout=httpx.Timeout(20.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def pay_url(self, payment_id: str) -> str:
        return f"{self.cfg.coinpay_base_url.rstrip('/')}/pay/{payment_id}"

    def create_payment(
        self,
        *,
        amount_usd: float,
        blockchain: str,
        description: str,
        metadata: dict[str, Any],
        redirect_url: str | None,
        idempotency_key: str,
    ) -> CreatedPayment:
        body: dict[str, Any] = {
            "business_id": self.cfg.coinpay_business_id,
            "amount": amount_usd,
            "currency": "USD",
            "blockchain": blockchain,
            "payment_method": "crypto",
            "description": description,
            "metadata": metadata,
        }
        if redirect_url:
            body["redirect_url"] = redirect_url
        res = self._client.post(
            "/api/payments/create",
            json=body,
            headers={
                "Authorization": f"Bearer {self.cfg.coinpay_api_key}",
                "Idempotency-Key": idempotency_key,
            },
        )
        if res.status_code >= 400:
            raise CoinPayError(f"coinpay {res.status_code}: {res.text[:300]}")
        data = res.json()
        payment = data.get("payment") if isinstance(data, dict) else None
        payment_id = (payment or {}).get("id") or (data.get("id") if isinstance(data, dict) else None)
        if not payment_id:
            raise CoinPayError("coinpay create response carried no payment id")
        return CreatedPayment(id=str(payment_id), pay_url=self.pay_url(str(payment_id)), raw=data)

    def get_payment(self, payment_id: str) -> dict[str, Any] | None:
        """Public, unauthenticated status read. ``None`` when CoinPay has no such payment."""
        res = self._client.get(f"/api/payments/{payment_id}")
        if res.status_code == 404:
            return None
        if res.status_code >= 400:
            raise CoinPayError(f"coinpay {res.status_code}: {res.text[:300]}")
        data = res.json()
        payment = data.get("payment") if isinstance(data, dict) else None
        return payment if isinstance(payment, dict) else None


def sign_webhook(raw_body: bytes, secret: str, ts: int) -> str:
    """Produce CoinPay's ``t=<ts>,v1=<hex>`` header. Used by tests and by anyone
    replaying a webhook by hand."""
    digest = hmac.new(secret.encode(), f"{ts}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def verify_webhook(
    raw_body: bytes, signature_header: str | None, secret: str, *, tolerance: int = 300, now: float | None = None
) -> bool:
    """HMAC-SHA256 over ``"<t>." + raw body``; the timestamp window stops replays.

    Verified over the raw bytes on purpose: re-serialising parsed JSON changes
    key order and whitespace and the signature stops matching.
    """
    if not secret or not signature_header:
        return False
    parts: dict[str, str] = {}
    for kv in signature_header.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            parts[k.strip()] = v.strip()
    ts_text, v1 = parts.get("t"), parts.get("v1")
    if not ts_text or not v1 or not ts_text.isdigit():
        return False
    ts = int(ts_text)
    current = now if now is not None else time.time()
    if abs(current - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts_text}.".encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, v1)


def is_settled(event: str | None, status: str | None) -> bool:
    return (event or "") in SETTLED_EVENTS or (status or "").lower() in SETTLED_STATUSES
