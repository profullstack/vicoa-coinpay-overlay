"""Plan arithmetic. Pure functions, no I/O, so they are trivially testable.

A crypto payment cannot be pulled again next month, so a "subscription" here is
a prepaid pass: paying buys ``days`` of Pro, stacked on top of whatever is left.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import CoinPayConfig

FREE = "free"
PRO = "pro"
PROVIDER = "coinpay"

INTERVALS = ("monthly", "annual")


@dataclass(frozen=True)
class Quote:
    plan_type: str
    billing_interval: str
    amount_usd: float
    days: int

    @property
    def label(self) -> str:
        return f"Vicoa Pro, {self.days} days ({self.billing_interval})"


def utcnow() -> datetime:
    """Naive UTC, matching how Vicoa's ``DateTime()`` columns are compared."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def as_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def iso_z(dt: datetime | None) -> str | None:
    dt = as_naive_utc(dt)
    return None if dt is None else dt.replace(microsecond=0).isoformat() + "Z"


def quote(cfg: CoinPayConfig, plan_type: str, billing_interval: str | None) -> Quote:
    if plan_type != PRO:
        raise ValueError(f"Unknown plan_type: {plan_type!r}")
    interval = billing_interval or "monthly"
    if interval == "monthly":
        return Quote(PRO, interval, round(cfg.coinpay_pro_monthly_usd, 2), cfg.coinpay_monthly_days)
    if interval == "annual":
        return Quote(PRO, interval, round(cfg.coinpay_pro_annual_usd, 2), cfg.coinpay_annual_days)
    raise ValueError(f"Unknown billing_interval: {billing_interval!r}")


def is_active(plan_type: str | None, current_period_end: datetime | None, now: datetime) -> bool:
    """A Pro pass is active until its end; a Pro row with no end never lapses."""
    if plan_type != PRO:
        return False
    end = as_naive_utc(current_period_end)
    return end is None or end > now


def next_period_end(current_period_end: datetime | None, active: bool, days: int, now: datetime) -> datetime:
    """Stack a new pass on the remaining time, or start from now if lapsed."""
    end = as_naive_utc(current_period_end)
    base = end if (active and end is not None and end > now) else now
    return base + timedelta(days=days)


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end
