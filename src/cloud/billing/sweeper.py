"""Background loop registered as a lifespan hook: lapse passes that ran out and
poll pending payments the webhook may have missed."""

from __future__ import annotations

import asyncio
import logging

from shared.database.session import SessionLocal

from . import coinpay as cp
from . import service
from .config import get_config

logger = logging.getLogger(__name__)


def sweep_once() -> tuple[int, int]:
    cfg = get_config()
    with SessionLocal() as db:
        lapsed = service.lapse_all_expired(db, cfg=cfg)
        granted = 0
        if cfg.coinpay_api_key and cfg.coinpay_business_id:
            client = cp.CoinPayClient(cfg)
            try:
                granted = service.reconcile_pending(db, client, cfg=cfg)
            finally:
                client.close()
    return lapsed, granted


class Sweeper:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="coinpay-sweeper")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        interval = max(10, get_config().coinpay_sweep_interval)
        while not self._stop.is_set():
            try:
                lapsed, granted = await asyncio.to_thread(sweep_once)
                if lapsed or granted:
                    logger.info("coinpay sweeper: lapsed=%d granted=%d", lapsed, granted)
            except Exception:
                logger.exception("coinpay sweeper failed; will retry")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
