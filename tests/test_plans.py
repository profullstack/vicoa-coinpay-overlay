from datetime import datetime, timedelta, timezone

import pytest

from cloud.billing import plans
from cloud.billing.config import CoinPayConfig


def test_quote_defaults_and_intervals():
    cfg = CoinPayConfig(coinpay_pro_monthly_usd=12, coinpay_pro_annual_usd=108)
    m = plans.quote(cfg, "pro", None)
    assert (m.amount_usd, m.days, m.billing_interval) == (12.0, 30, "monthly")
    a = plans.quote(cfg, "pro", "annual")
    assert (a.amount_usd, a.days) == (108.0, 365)
    with pytest.raises(ValueError):
        plans.quote(cfg, "enterprise", None)
    with pytest.raises(ValueError):
        plans.quote(cfg, "pro", "weekly")


def test_active_and_stacking():
    now = datetime(2026, 9, 5, 12, 0, 0)
    assert plans.is_active("pro", now + timedelta(days=1), now)
    assert plans.is_active("pro", None, now)
    assert not plans.is_active("pro", now, now)
    assert not plans.is_active("free", now + timedelta(days=9), now)
    # remaining time is kept
    end = now + timedelta(days=10)
    assert plans.next_period_end(end, True, 30, now) == end + timedelta(days=30)
    # a lapsed pass restarts from now
    assert plans.next_period_end(now - timedelta(days=1), False, 30, now) == now + timedelta(days=30)
    # aware datetimes are normalised to naive UTC
    aware = (now + timedelta(days=2)).replace(tzinfo=timezone.utc)
    assert plans.next_period_end(aware, True, 1, now) == now + timedelta(days=3)


def test_month_bounds_and_iso():
    s, e = plans.month_bounds(datetime(2026, 12, 25, 3, 4, 5))
    assert (s, e) == (datetime(2026, 12, 1), datetime(2027, 1, 1))
    assert plans.iso_z(datetime(2026, 1, 2, 3, 4, 5, 999)) == "2026-01-02T03:04:05Z"
    assert plans.iso_z(None) is None
