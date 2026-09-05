"""CoinPay billing overlay for self-hosted Vicoa.

Vicoa's backend does ``import cloud`` when a package by that name is on the
path (see ``backend/main.py``), then fires the registries in
``shared.hooks``. Importing this module registers:

* an app-setup hook that mounts ``/api/v1/billing/*``, wraps the app in the
  free-plan gate, and ensures the overlay's schema;
* a lifespan hook running the sweeper;
* a user-delete hook that removes billing rows before the ``users`` row goes.

Nothing here talks to the network or the database at import time, so the
alembic ``migrate`` service can import ``cloud.billing.models`` safely.
"""

from __future__ import annotations

import logging

from shared.hooks import register_app_setup, register_lifespan_hook, register_user_delete_hook

logger = logging.getLogger(__name__)

__version__ = "0.1.0"


@register_app_setup
def _setup(app) -> None:
    from shared.config.settings import settings

    from .billing.config import get_config
    from .billing.gate import FreePlanGate
    from .billing.routes import router

    cfg = get_config()
    if cfg.coinpay_auto_schema:
        try:
            from shared.database.session import engine

            from .billing.schema import ensure_schema

            ensure_schema(engine)
        except Exception:
            logger.exception("coinpay overlay: could not ensure schema; billing routes may fail")
    app.include_router(router, prefix=settings.api_v1_prefix)
    app.add_middleware(FreePlanGate)
    logger.info(
        "coinpay overlay %s loaded (checkout %s, free limit %s msgs/month, chains %s)",
        __version__, "enabled" if cfg.checkout_enabled else "NOT configured",
        cfg.coinpay_free_message_limit or "off", ",".join(cfg.blockchains),
    )


class _SweeperHook:
    def __init__(self) -> None:
        from .billing.sweeper import Sweeper

        self._sweeper = Sweeper()

    async def start(self) -> None:
        await self._sweeper.start()

    async def stop(self) -> None:
        await self._sweeper.stop()


register_lifespan_hook(_SweeperHook())


@register_user_delete_hook
def _delete_user(db, user_id) -> None:
    from .billing.service import delete_user_billing

    delete_user_billing(db, user_id)
