"""Test wiring.

Unit tests run anywhere. Integration tests need two things:

* ``VICOA_SRC``: path to a Vicoa checkout's ``backend/src`` (the overlay imports
  ``shared.*`` and ``backend.*`` from it);
* ``TEST_DATABASE_URL``: a Postgres database already migrated with Vicoa's
  ``alembic upgrade head``.

Both are set by ``make test`` and by CI; missing either skips the integration
suite instead of failing it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

VICOA_SRC = os.environ.get("VICOA_SRC", "")
TEST_DB = os.environ.get("TEST_DATABASE_URL", "")
HAVE_DB = bool(VICOA_SRC and TEST_DB and Path(VICOA_SRC, "shared").is_dir())

if VICOA_SRC:
    sys.path.insert(0, VICOA_SRC)

# Vicoa's Settings and the engine read the environment at import time; set it
# before anything imports shared.database.session.
os.environ.setdefault("ENVIRONMENT", "test")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB
os.environ.update({
    "COINPAY_BASE_URL": "https://coinpay.test",
    "COINPAY_API_KEY": "cp_test_" + "a" * 32,
    "COINPAY_BUSINESS_ID": "biz-123",
    "COINPAY_WEBHOOK_SECRET": "whsec-test",
    "COINPAY_BLOCKCHAINS": "USDC_BASE,BTC",
    "COINPAY_PRO_MONTHLY_USD": "12",
    "COINPAY_PRO_ANNUAL_USD": "108",
    "COINPAY_FREE_MESSAGE_LIMIT": "3",
    "PUBLIC_BACKEND_URL": "http://backend.test:8000",
    "PUBLIC_WEB_URL": "http://web.test:3000",
})

integration = pytest.mark.skipif(not HAVE_DB, reason="needs VICOA_SRC and TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _fresh_config():
    from cloud.billing.config import reset_config_cache

    reset_config_cache()
    yield
    reset_config_cache()
