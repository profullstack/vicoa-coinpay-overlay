"""Everything that touches the database. Routes, the gate and the sweeper are
thin wrappers around these functions so there is exactly one code path that
can turn money into Pro (:func:`grant`)."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from shared.database.enums import SenderType
from shared.database.models import AgentInstance, Message

from . import coinpay as cp
from .config import CoinPayConfig, get_config
from .intent import Intent
from .models import BillingEvent, CoinPayPayment, Subscription
from .plans import FREE, PRO, PROVIDER, Quote, is_active, month_bounds, next_period_end, utcnow

logger = logging.getLogger(__name__)


# --- subscriptions --------------------------------------------------------


def get_or_create_subscription(db: Session, user_id: UUID, cfg: CoinPayConfig | None = None) -> Subscription:
    cfg = cfg or get_config()
    sub = db.execute(select(Subscription).where(Subscription.user_id == user_id)).scalar_one_or_none()
    if sub is not None:
        return sub
    sub = Subscription(user_id=user_id, plan_type=FREE, agent_limit=cfg.coinpay_free_agent_limit)
    try:
        with db.begin_nested():
            db.add(sub)
            db.flush()
    except IntegrityError:
        # A concurrent first request won the unique(user_id) race.
        return db.execute(select(Subscription).where(Subscription.user_id == user_id)).scalar_one()
    db.commit()
    return sub


def effective(db: Session, sub: Subscription, now: datetime | None = None, cfg: CoinPayConfig | None = None) -> Subscription:
    """Lapse a Pro row whose pass has run out. Persisted so every reader agrees."""
    now = now or utcnow()
    if sub.plan_type == PRO and not is_active(sub.plan_type, sub.current_period_end, now):
        cfg = cfg or get_config()
        lapse(db, sub, cfg, now)
    return sub


def lapse(db: Session, sub: Subscription, cfg: CoinPayConfig, now: datetime) -> None:
    sub.plan_type = FREE
    sub.agent_limit = cfg.coinpay_free_agent_limit
    sub.cancel_at_period_end = False
    sub.updated_at = now
    db.add(BillingEvent(user_id=sub.user_id, subscription_id=sub.id, event_type="subscription.lapsed", occurred_at=now,
                        event_data=json.dumps({"provider": sub.provider, "payment_id": sub.provider_subscription_id})))
    db.commit()


def cancel(db: Session, sub: Subscription, now: datetime | None = None) -> Subscription:
    """Nothing to stop: a pass is prepaid. This only records the intent so the
    UI can say "ends on <date>" instead of "renews"."""
    now = now or utcnow()
    if sub.plan_type == PRO and not sub.cancel_at_period_end:
        sub.cancel_at_period_end = True
        sub.updated_at = now
        db.add(BillingEvent(user_id=sub.user_id, subscription_id=sub.id, event_type="subscription.cancel_scheduled", occurred_at=now))
        db.commit()
    return sub


def lapse_all_expired(db: Session, now: datetime | None = None, cfg: CoinPayConfig | None = None) -> int:
    now = now or utcnow()
    cfg = cfg or get_config()
    rows = db.execute(
        select(Subscription).where(
            Subscription.plan_type == PRO,
            Subscription.provider == PROVIDER,
            Subscription.current_period_end.is_not(None),
            Subscription.current_period_end <= now,
        )
    ).scalars().all()
    for sub in rows:
        lapse(db, sub, cfg, now)
    return len(rows)


# --- usage ------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    total_agents: int
    agent_limit: int
    messages_used: int
    message_limit: int
    period_start: datetime
    period_end: datetime


def usage(db: Session, user_id: UUID, sub: Subscription, now: datetime | None = None, cfg: CoinPayConfig | None = None) -> Usage:
    now = now or utcnow()
    cfg = cfg or get_config()
    start, end = month_bounds(now)
    agents = db.execute(
        select(func.count()).select_from(AgentInstance).where(
            AgentInstance.user_id == user_id, AgentInstance.started_at >= start, AgentInstance.started_at < end
        )
    ).scalar_one()
    msgs = messages_this_month(db, user_id, now)
    pro = sub.plan_type == PRO
    return Usage(
        total_agents=int(agents),
        agent_limit=sub.agent_limit,
        messages_used=msgs,
        message_limit=-1 if pro or cfg.coinpay_free_message_limit <= 0 else cfg.coinpay_free_message_limit,
        period_start=start,
        period_end=end,
    )


def messages_this_month(db: Session, user_id: UUID, now: datetime | None = None) -> int:
    start, end = month_bounds(now or utcnow())
    return int(
        db.execute(
            select(func.count()).select_from(Message).where(
                Message.sender_user_id == user_id,
                Message.sender_type == SenderType.USER,
                Message.created_at >= start,
                Message.created_at < end,
            )
        ).scalar_one()
    )


def over_free_limit(db: Session, user_id: UUID, cfg: CoinPayConfig | None = None, now: datetime | None = None) -> tuple[bool, int, int]:
    """(blocked, used, limit) for the message gate."""
    cfg = cfg or get_config()
    now = now or utcnow()
    limit = cfg.coinpay_free_message_limit
    if limit <= 0:
        return False, 0, -1
    sub = get_or_create_subscription(db, user_id, cfg)
    effective(db, sub, now, cfg)
    if sub.plan_type == PRO:
        return False, 0, -1
    used = messages_this_month(db, user_id, now)
    return used >= limit, used, limit


# --- payments ---------------------------------------------------------------


def start_payment(
    db: Session, client: cp.CoinPayClient, cfg: CoinPayConfig, intent: Intent, quote: Quote, blockchain: str
) -> CoinPayPayment:
    """Create the CoinPay payment for an intent, once. A refresh of the picker
    page with the same chain reuses the pending payment."""
    if blockchain not in cfg.blockchains:
        raise ValueError(f"blockchain {blockchain!r} is not offered")
    user_id = UUID(intent.user_id)
    now = utcnow()
    existing = db.execute(
        select(CoinPayPayment).where(
            CoinPayPayment.intent_id == intent.jti,
            CoinPayPayment.blockchain == blockchain,
            CoinPayPayment.status == "pending",
            CoinPayPayment.created_at >= now - timedelta(minutes=14),
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    created = client.create_payment(
        amount_usd=quote.amount_usd,
        blockchain=blockchain,
        description=quote.label,
        metadata={
            "vicoa_user_id": intent.user_id,
            "plan_type": quote.plan_type,
            "billing_interval": quote.billing_interval,
            "days": quote.days,
            "intent_id": intent.jti,
        },
        redirect_url=intent.success_url or None,
        idempotency_key=f"vicoa-{intent.jti}-{blockchain}",
    )
    row = CoinPayPayment(
        user_id=user_id,
        coinpay_payment_id=created.id,
        intent_id=intent.jti,
        plan_type=quote.plan_type,
        billing_interval=quote.billing_interval,
        days=quote.days,
        amount_usd=quote.amount_usd,
        blockchain=blockchain,
        status="pending",
        pay_url=created.pay_url,
        raw=created.raw,
    )
    db.add(row)
    db.add(BillingEvent(user_id=user_id, event_type="checkout.started", provider_event_id=created.id, occurred_at=now,
                        event_data=json.dumps({"amount_usd": quote.amount_usd, "blockchain": blockchain, "days": quote.days})))
    db.commit()
    return row


def find_payment(db: Session, coinpay_payment_id: str) -> CoinPayPayment | None:
    return db.execute(
        select(CoinPayPayment).where(CoinPayPayment.coinpay_payment_id == coinpay_payment_id)
    ).scalar_one_or_none()


@dataclass(frozen=True)
class Outcome:
    granted: bool
    reason: str
    current_period_end: datetime | None = None


def apply_status(
    db: Session, payment: CoinPayPayment, *, event: str | None, status: str | None, raw: dict[str, Any] | None,
    cfg: CoinPayConfig | None = None, now: datetime | None = None,
) -> Outcome:
    """Record what CoinPay says about a payment and grant if it settled.

    Idempotent: a settled payment grants exactly once no matter how many
    webhooks or polls report it.
    """
    cfg = cfg or get_config()
    now = now or utcnow()
    status_l = (status or "").lower()
    if status_l:
        payment.status = status_l
    if raw is not None:
        payment.raw = raw
    payment.updated_at = now

    if payment.granted_at is not None:
        db.commit()
        return Outcome(False, "already granted", _period_end(db, payment.user_id))

    if not cp.is_settled(event, status):
        db.commit()
        return Outcome(False, f"not settled ({status_l or event or 'unknown'})")

    end = grant(db, payment, cfg, now)
    return Outcome(True, "granted", end)


def grant(db: Session, payment: CoinPayPayment, cfg: CoinPayConfig, now: datetime) -> datetime:
    """The only place money becomes Pro."""
    sub = get_or_create_subscription(db, payment.user_id, cfg)
    active = is_active(sub.plan_type, sub.current_period_end, now)
    end = next_period_end(sub.current_period_end, active, payment.days, now)
    sub.plan_type = PRO
    sub.provider = PROVIDER
    sub.provider_subscription_id = payment.coinpay_payment_id
    sub.agent_limit = cfg.coinpay_pro_agent_limit
    sub.current_period_end = end
    sub.cancel_at_period_end = False
    sub.updated_at = now
    payment.granted_at = now
    if payment.status not in cp.SETTLED_STATUSES:
        payment.status = "confirmed"
    db.add(BillingEvent(
        user_id=payment.user_id, subscription_id=sub.id, event_type="subscription.granted",
        provider_event_id=payment.coinpay_payment_id, occurred_at=now,
        event_data=json.dumps({"days": payment.days, "amount_usd": payment.amount_usd,
                               "blockchain": payment.blockchain, "current_period_end": end.isoformat()}),
    ))
    db.commit()
    logger.info("coinpay: granted %s days of pro to user %s (payment %s)", payment.days, payment.user_id, payment.coinpay_payment_id)
    return end


def _period_end(db: Session, user_id: UUID) -> datetime | None:
    sub = db.execute(select(Subscription).where(Subscription.user_id == user_id)).scalar_one_or_none()
    return sub.current_period_end if sub else None


def reconcile_pending(db: Session, client: cp.CoinPayClient, cfg: CoinPayConfig | None = None, now: datetime | None = None, max_age: timedelta = timedelta(hours=2)) -> int:
    """Poll CoinPay for pending payments the webhook may not have reached us
    about (a localhost trial, a webhook URL typo). Returns grants made."""
    cfg = cfg or get_config()
    now = now or utcnow()
    rows = db.execute(
        select(CoinPayPayment).where(
            CoinPayPayment.status == "pending", CoinPayPayment.granted_at.is_(None),
            CoinPayPayment.created_at >= now - max_age,
        )
    ).scalars().all()
    granted = 0
    for row in rows:
        try:
            remote = client.get_payment(row.coinpay_payment_id)
        except cp.CoinPayError as exc:
            logger.warning("coinpay: poll %s failed: %s", row.coinpay_payment_id, exc)
            continue
        if remote is None:
            continue
        out = apply_status(db, row, event=None, status=remote.get("status"), raw=remote, cfg=cfg, now=now)
        granted += int(out.granted)
    return granted


def delete_user_billing(db: Session, user_id: UUID) -> None:
    """``run_user_delete_hooks`` callback. ``subscriptions`` / ``billing_events``
    reference ``users`` without ON DELETE CASCADE, so without this the open
    core's account deletion would fail for anyone who ever had a row."""
    for model in (CoinPayPayment, BillingEvent, Subscription):
        for row in db.execute(select(model).where(model.user_id == user_id)).scalars().all():
            db.delete(row)
    db.flush()
