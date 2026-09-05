"""Signed checkout intents.

``POST /billing/checkout`` is authenticated with a bearer token, but the page it
sends the buyer to is opened by a plain browser navigation (the desktop app
shells out to the system browser). The intent carries who is buying what,
signed so the picker page can create the payment without a session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import asdict, dataclass


class IntentError(ValueError):
    pass


@dataclass(frozen=True)
class Intent:
    jti: str
    user_id: str
    plan_type: str
    billing_interval: str
    success_url: str
    cancel_url: str
    exp: int  # unix seconds


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sig(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def new_intent(
    *,
    user_id: str,
    plan_type: str,
    billing_interval: str,
    success_url: str,
    cancel_url: str,
    ttl_seconds: int,
    now: float | None = None,
) -> Intent:
    return Intent(
        jti=secrets.token_urlsafe(12),
        user_id=user_id,
        plan_type=plan_type,
        billing_interval=billing_interval,
        success_url=success_url,
        cancel_url=cancel_url,
        exp=int((now if now is not None else time.time()) + ttl_seconds),
    )


def encode(intent: Intent, secret: str) -> str:
    if not secret:
        raise IntentError("checkout secret is not configured")
    payload = _b64e(json.dumps(asdict(intent), separators=(",", ":"), sort_keys=True).encode())
    return f"{payload}.{_sig(secret, payload)}"


def decode(token: str, secret: str, now: float | None = None) -> Intent:
    if not secret:
        raise IntentError("checkout secret is not configured")
    try:
        payload, sig = token.split(".", 1)
    except ValueError as exc:
        raise IntentError("malformed token") from exc
    if not hmac.compare_digest(_sig(secret, payload), sig):
        raise IntentError("bad signature")
    try:
        data = json.loads(_b64d(payload))
        intent = Intent(**data)
    except (ValueError, TypeError) as exc:
        raise IntentError("malformed payload") from exc
    if intent.exp < (now if now is not None else time.time()):
        raise IntentError("checkout link expired")
    return intent
