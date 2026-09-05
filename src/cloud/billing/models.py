"""SQLAlchemy models for the billing tables.

``subscriptions`` and ``billing_events`` already exist in every Vicoa database
(they come from the retained historical migrations); the open core just ships
no model for them. This module attaches ``User.subscription`` /
``User.billing_events`` via ``backref`` exactly as the closed overlay does, and
adds one table of our own, ``coinpay_payments``, plus the two columns the web
client reads (``current_period_end``, ``cancel_at_period_end``) which the
historical migrations never created. :mod:`cloud.billing.schema` creates them.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgresUUID
from sqlalchemy.orm import Mapped, backref, mapped_column, relationship

from shared.database.models import Base, User

from .plans import utcnow


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"), type_=PostgresUUID(as_uuid=True), unique=True
    )
    plan_type: Mapped[str] = mapped_column(String(50), default="free")
    agent_limit: Mapped[int] = mapped_column(Integer, default=10)
    # ``stripe`` / ``apple`` / ``google`` on vicoa.ai; ``coinpay`` here.
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True, default=None)
    provider_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    # The CoinPay payment id of the pass currently in force.
    provider_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    user: Mapped[User] = relationship(
        "User", backref=backref("subscription", uselist=False, cascade="all, delete-orphan")
    )


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), type_=PostgresUUID(as_uuid=True))
    subscription_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("subscriptions.id"), type_=PostgresUUID(as_uuid=True), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100))
    event_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(
        "User", backref=backref("billing_events", cascade="all, delete-orphan")
    )


class CoinPayPayment(Base):
    """One row per CoinPay payment we created. The webhook and the sweeper both
    resolve payments through this table, so a payment we never created cannot
    grant anything even with a valid signature."""

    __tablename__ = "coinpay_payments"
    __table_args__ = (
        Index("ix_coinpay_payments_user_created", "user_id", "created_at"),
        Index("ix_coinpay_payments_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgresUUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), type_=PostgresUUID(as_uuid=True)
    )
    coinpay_payment_id: Mapped[str] = mapped_column(String(255), unique=True)
    intent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    plan_type: Mapped[str] = mapped_column(String(50))
    billing_interval: Mapped[str] = mapped_column(String(20))
    days: Mapped[int] = mapped_column(Integer)
    amount_usd: Mapped[float] = mapped_column(Float)
    blockchain: Mapped[str] = mapped_column(String(20))
    # pending | confirmed | forwarded | expired | failed
    status: Mapped[str] = mapped_column(String(32), default="pending")
    pay_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
