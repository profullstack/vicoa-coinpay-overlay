"""End to end against a real Vicoa schema: routes, webhook, gate, sweeper, delete hook."""

from __future__ import annotations

import json
import time
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from conftest import HAVE_DB, integration

if HAVE_DB:
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import text

    from backend.auth.dependencies import get_current_user
    from shared.auth.base import Principal
    from shared.database.enums import AgentStatus, SenderType
    from shared.database.models import AgentInstance, Message, User, UserAgent
    from shared.database.session import SessionLocal, engine

    from cloud.billing import coinpay as cp
    from cloud.billing import gate, routes, service
    from cloud.billing.config import get_config
    from cloud.billing.models import BillingEvent, CoinPayPayment, Subscription
    from cloud.billing.plans import utcnow
    from cloud.billing.schema import ensure_schema
    from cloud.billing.sweeper import sweep_once

pytestmark = integration


class FakeCoinPay:
    """Stands in for coinpayportal.com: remembers created payments, lets a test
    flip their status, and serves the public status read."""

    def __init__(self):
        self.payments: dict[str, dict] = {}
        self.creates = 0
        self.fail_create = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/payments/create":
            self.creates += 1
            if self.fail_create:
                return httpx.Response(500, json={"success": False, "error": "boom"})
            body = json.loads(request.content)
            pid = f"pay_{len(self.payments) + 1}"
            self.payments[pid] = {"id": pid, "status": "pending", "amount": body["amount"], "blockchain": body["blockchain"],
                                  "metadata": body.get("metadata", {})}
            return httpx.Response(200, json={"success": True, "payment": self.payments[pid], "usage": {}})
        if request.url.path.startswith("/api/payments/"):
            pid = request.url.path.rsplit("/", 1)[1]
            if pid not in self.payments:
                return httpx.Response(404, json={"success": False})
            return httpx.Response(200, json={"success": True, "payment": self.payments[pid]})
        return httpx.Response(404)


@pytest.fixture(scope="module", autouse=True)
def _schema():
    ensure_schema(engine)


@pytest.fixture
def db():
    with SessionLocal() as s:
        yield s


@pytest.fixture
def user(db):
    u = User(id=uuid4(), email=f"{uuid4().hex}@test.local", display_name="Tester")
    db.add(u)
    db.commit()
    yield u
    # teardown mirrors the overlay's delete hook, then removes the user
    with SessionLocal() as s:
        service.delete_user_billing(s, u.id)
        s.execute(text("DELETE FROM messages WHERE sender_user_id = :u"), {"u": u.id})
        s.execute(text("DELETE FROM agent_instances WHERE user_id = :u"), {"u": u.id})
        s.execute(text("DELETE FROM user_agents WHERE user_id = :u"), {"u": u.id})
        s.execute(text("DELETE FROM users WHERE id = :u"), {"u": u.id})
        s.commit()


@pytest.fixture
def fake():
    return FakeCoinPay()


@pytest.fixture
def app(user, fake, monkeypatch):
    routes.set_client(cp.CoinPayClient(get_config(), transport=httpx.MockTransport(fake.handler)))
    application = FastAPI()
    application.include_router(routes.router, prefix="/api/v1")

    @application.post("/api/v1/agent-instances/{instance_id}/messages")
    def fake_send(instance_id: str):
        return {"ok": True}

    application.dependency_overrides[get_current_user] = lambda: user
    application.add_middleware(gate.FreePlanGate)
    # the gate resolves the bearer itself; make "tok-<uuid>" resolve to that user
    monkeypatch.setattr(gate, "verify_user_token", lambda tok: Principal(user_id=tok.removeprefix("tok-")))
    yield application
    routes.set_client(None)


@pytest.fixture
def client(app, user):
    return TestClient(app, headers={"Authorization": f"Bearer tok-{user.id}"})


def _webhook(client, payload: dict, secret="whsec-test", ts=None):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    sig = cp.sign_webhook(raw, secret, ts or int(time.time()))
    return client.post("/api/v1/billing/coinpay/webhook", content=raw, headers={"X-CoinPay-Signature": sig, "Content-Type": "application/json"})


def _buy(client, fake, interval="monthly", chain="USDC_BASE", success="http://web.test:3000/dashboard/settings?tab=billing&checkout=success"):
    r = client.post("/api/v1/billing/checkout", json={"plan_type": "pro", "billing_interval": interval, "success_url": success,
                                                      "cancel_url": "http://web.test:3000/pricing?cancelled=1"})
    assert r.status_code == 200, r.text
    url = r.json()["checkout_url"]
    assert url.startswith("http://backend.test:8000/api/v1/billing/coinpay/checkout/")
    path = url.removeprefix("http://backend.test:8000")
    page = client.get(path)
    assert page.status_code == 200 and "Vicoa Pro" in page.text and "USDC on Base" in page.text and "Bitcoin" in page.text
    r = client.post(path, data={"blockchain": chain}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("https://coinpay.test/pay/pay_")
    return r.headers["location"].rsplit("/", 1)[1], path


def test_free_by_default_and_usage(client, user):
    r = client.get("/api/v1/billing/subscription")
    assert r.status_code == 200
    body = r.json()
    assert body["plan_type"] == "free" and body["provider"] is None and body["current_period_end"] is None
    assert body["agent_limit"] == 10 and body["cancel_at_period_end"] is False
    u = client.get("/api/v1/billing/usage").json()
    assert u["total_agents"] == 0 and u["messages_used"] == 0 and u["message_limit"] == 3
    m = client.get("/api/v1/billing/mobile/status").json()
    assert m == {"plan_type": "free", "provider": None, "is_pro": False, "current_period_end": None, "cancel_at_period_end": False}
    assert client.post("/api/v1/billing/cancel").status_code == 400


def test_checkout_webhook_grants_pro_once(client, user, fake, db):
    pid, path = _buy(client, fake)
    assert fake.creates == 1
    assert fake.payments[pid]["metadata"]["vicoa_user_id"] == str(user.id)
    # re-posting the same intent + chain reuses the pending payment
    r = client.post(path, data={"blockchain": "USDC_BASE"}, follow_redirects=False)
    assert r.status_code == 303 and fake.creates == 1

    # a pending webhook grants nothing
    r = _webhook(client, {"event": "payment.detected", "payment_id": pid, "business_id": "biz-123", "status": "pending"})
    assert r.json()["granted"] is False
    assert client.get("/api/v1/billing/subscription").json()["plan_type"] == "free"

    # confirmed grants 30 days
    r = _webhook(client, {"event": "payment.confirmed", "payment_id": pid, "business_id": "biz-123", "status": "confirmed",
                          "amount_usd": "12", "tx_hash": "0xabc"})
    assert r.status_code == 200 and r.json()["granted"] is True, r.text
    sub = client.get("/api/v1/billing/subscription").json()
    assert sub["plan_type"] == "pro" and sub["provider"] == "coinpay"
    end1 = sub["current_period_end"]
    assert end1 is not None

    # forwarded (second webhook for the same payment) does not double-grant
    r = _webhook(client, {"event": "payment.forwarded", "payment_id": pid, "business_id": "biz-123", "status": "forwarded"})
    assert r.json() == {"received": True, "known": True, "granted": False, "reason": "already granted", "current_period_end": end1}
    assert client.get("/api/v1/billing/subscription").json()["current_period_end"] == end1

    with SessionLocal() as s:
        row = s.query(CoinPayPayment).filter_by(coinpay_payment_id=pid).one()
        assert row.status == "forwarded" and row.granted_at is not None
        kinds = [e.event_type for e in s.query(BillingEvent).filter_by(user_id=user.id).all()]
        assert kinds.count("subscription.granted") == 1 and "checkout.started" in kinds

    # a second purchase stacks on the remaining time
    pid2, _ = _buy(client, fake, interval="annual", chain="BTC")
    _webhook(client, {"event": "payment.confirmed", "payment_id": pid2, "business_id": "biz-123", "status": "confirmed"})
    sub = client.get("/api/v1/billing/subscription").json()
    from datetime import datetime
    d1 = datetime.fromisoformat(end1.rstrip("Z"))
    d2 = datetime.fromisoformat(sub["current_period_end"].rstrip("Z"))
    assert d2 - d1 == timedelta(days=365)

    # cancel is only bookkeeping
    r = client.post("/api/v1/billing/cancel")
    assert r.status_code == 200 and r.json()["cancel_at_period_end"] is True
    sub = client.get("/api/v1/billing/subscription").json()
    assert sub["plan_type"] == "pro" and sub["cancel_at_period_end"] is True


def test_webhook_rejections(client, fake):
    pid, _ = _buy(client, fake)
    good = {"event": "payment.confirmed", "payment_id": pid, "business_id": "biz-123", "status": "confirmed"}
    assert _webhook(client, good, secret="wrong").status_code == 401
    assert _webhook(client, good, ts=int(time.time()) - 1000).status_code == 401
    raw = json.dumps(good).encode()
    assert client.post("/api/v1/billing/coinpay/webhook", content=raw).status_code == 401
    # other business
    r = _webhook(client, dict(good, business_id="other"))
    assert r.status_code == 400
    # a payment we never created is acknowledged and ignored
    r = _webhook(client, dict(good, payment_id="pay_999"))
    assert r.status_code == 200 and r.json() == {"received": True, "known": False}
    assert client.get("/api/v1/billing/subscription").json()["plan_type"] == "free"


def test_checkout_validation(client):
    r = client.post("/api/v1/billing/checkout", json={"plan_type": "pro", "success_url": "https://evil.example/x", "cancel_url": ""})
    assert r.status_code == 400
    r = client.post("/api/v1/billing/checkout", json={"plan_type": "enterprise", "success_url": "", "cancel_url": ""})
    assert r.status_code == 400
    r = client.get("/api/v1/billing/coinpay/checkout/garbage.token")
    assert r.status_code == 400 and "not valid" in r.text
    r = client.post("/api/v1/billing/coinpay/checkout/garbage.token", data={"blockchain": "BTC"})
    assert r.status_code == 400
    cfg = client.get("/api/v1/billing/coinpay/config").json()
    assert cfg["enabled"] is True and [c["code"] for c in cfg["blockchains"]] == ["USDC_BASE", "BTC"]
    assert cfg["plans"]["pro"]["annual"] == {"amount_usd": 108.0, "days": 365}


def test_checkout_unknown_chain_and_coinpay_down(client, fake):
    r = client.post("/api/v1/billing/checkout", json={"plan_type": "pro", "success_url": "", "cancel_url": ""})
    path = r.json()["checkout_url"].removeprefix("http://backend.test:8000")
    r = client.post(path, data={"blockchain": "DOGE"}, follow_redirects=False)
    assert r.status_code == 400 and "Pick one" in r.text
    fake.fail_create = True
    r = client.post(path, data={"blockchain": "BTC"}, follow_redirects=False)
    assert r.status_code == 502 and "could not start" in r.text


def test_portal_is_a_renewal_checkout(client):
    r = client.post("/api/v1/billing/portal", json={"return_url": "http://web.test:3000/dashboard/settings?tab=billing"})
    assert r.status_code == 200 and "/billing/coinpay/checkout/" in r.json()["url"]


def test_poll_reconciles_without_webhook(client, fake):
    pid, _ = _buy(client, fake)
    r = client.get(f"/api/v1/billing/coinpay/payments/{pid}")
    assert r.json()["status"] == "pending" and r.json()["granted"] is False
    fake.payments[pid]["status"] = "confirmed"
    r = client.get(f"/api/v1/billing/coinpay/payments/{pid}")
    assert r.json()["granted"] is True and r.json()["plan_type"] == "pro"
    assert client.get("/api/v1/billing/coinpay/payments/pay_404").status_code == 404


def test_sweeper_polls_and_lapses(client, fake, user, monkeypatch):
    pid, _ = _buy(client, fake)
    fake.payments[pid]["status"] = "confirmed"
    real = cp.CoinPayClient
    monkeypatch.setattr(cp, "CoinPayClient", lambda cfg, transport=None: real(cfg, transport=httpx.MockTransport(fake.handler)))
    lapsed, granted = sweep_once()
    assert granted == 1
    assert client.get("/api/v1/billing/subscription").json()["plan_type"] == "pro"
    with SessionLocal() as s:
        sub = s.query(Subscription).filter_by(user_id=user.id).one()
        sub.current_period_end = utcnow() - timedelta(seconds=1)
        s.commit()
    lapsed, granted = sweep_once()
    assert lapsed == 1
    sub = client.get("/api/v1/billing/subscription").json()
    assert sub["plan_type"] == "free" and sub["agent_limit"] == 10 and sub["provider"] == "coinpay"


def test_read_path_lapses_expired_pass(client, user):
    with SessionLocal() as s:
        sub = service.get_or_create_subscription(s, user.id)
        sub.plan_type, sub.provider, sub.current_period_end = "pro", "coinpay", utcnow() - timedelta(days=1)
        s.commit()
    assert client.get("/api/v1/billing/mobile/status").json()["is_pro"] is False


def test_gate_blocks_free_after_limit_and_lets_pro_through(client, user, fake, db):
    ua = UserAgent(user_id=user.id, name="claude")
    db.add(ua)
    db.flush()
    inst = AgentInstance(user_agent_id=ua.id, user_id=user.id, status=AgentStatus.ACTIVE)
    db.add(inst)
    db.commit()
    for _ in range(3):
        db.add(Message(agent_instance_id=inst.id, sender_type=SenderType.USER, sender_user_id=user.id, content="hi"))
    db.commit()
    assert client.get("/api/v1/billing/usage").json()["messages_used"] == 3
    assert client.get("/api/v1/billing/usage").json()["total_agents"] == 1
    r = client.post(f"/api/v1/agent-instances/{inst.id}/messages", json={"content": "x"})
    assert r.status_code == 402
    assert r.json()["code"] == "free_plan_limit" and r.json()["upgrade_url"] == "http://web.test:3000/dashboard/upgrade"
    # other routes are untouched
    assert client.get("/api/v1/billing/subscription").status_code == 200
    # buy pro -> unblocked
    pid, _ = _buy(client, fake)
    _webhook(client, {"event": "payment.confirmed", "payment_id": pid, "business_id": "biz-123", "status": "confirmed"})
    assert client.post(f"/api/v1/agent-instances/{inst.id}/messages", json={"content": "x"}).status_code == 200
    assert client.get("/api/v1/billing/usage").json()["message_limit"] == -1


def test_gate_fails_open_on_error(client, user, monkeypatch):
    def boom(tok):
        raise RuntimeError("idp down")
    monkeypatch.setattr(gate, "verify_user_token", boom)
    assert client.post(f"/api/v1/agent-instances/{uuid4()}/messages", json={"content": "x"}).status_code == 200


def test_gate_off_when_limit_zero(client, user, db, monkeypatch):
    monkeypatch.setenv("COINPAY_FREE_MESSAGE_LIMIT", "0")
    get_config.cache_clear()
    assert client.post(f"/api/v1/agent-instances/{uuid4()}/messages", json={"content": "x"}).status_code == 200


def test_delete_hook_removes_billing_rows(client, user, fake):
    pid, _ = _buy(client, fake)
    _webhook(client, {"event": "payment.confirmed", "payment_id": pid, "business_id": "biz-123", "status": "confirmed"})
    with SessionLocal() as s:
        service.delete_user_billing(s, user.id)
        s.commit()
        assert s.query(Subscription).filter_by(user_id=user.id).count() == 0
        assert s.query(BillingEvent).filter_by(user_id=user.id).count() == 0
        assert s.query(CoinPayPayment).filter_by(user_id=user.id).count() == 0
