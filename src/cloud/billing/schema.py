"""Idempotent DDL run at startup.

The overlay cannot add files to Vicoa's alembic ``versions/`` directory from
outside the image, and the historical migrations never created the two period
columns the web client reads. ``ADD COLUMN IF NOT EXISTS`` and ``CREATE TABLE
IF NOT EXISTS`` are safe to run on every boot.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .models import CoinPayPayment

logger = logging.getLogger(__name__)

_ALTERS = (
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS provider VARCHAR(20)",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS current_period_end TIMESTAMP",
    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE",
)


def ensure_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        for stmt in _ALTERS:
            conn.execute(text(stmt))
    CoinPayPayment.__table__.create(engine, checkfirst=True)
    logger.info("coinpay overlay schema ensured")
