"""The ``/api/v1/billing/*`` surface Vicoa's web, desktop and mobile clients
already call, plus the CoinPay-specific checkout page and webhook."""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.auth.dependencies import get_current_user
from shared.config.settings import settings as vicoa_settings
from shared.database.models import User
from shared.database.session import get_db

from . import coinpay as cp
from . import intent as intents
from . import service
from .config import CoinPayConfig, get_config
from .pages import error_page, picker_page
from .plans import PRO, iso_z, quote

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

# One client per process; httpx pools connections.
_client: cp.CoinPayClient | None = None


def get_client(cfg: CoinPayConfig = Depends(get_config)) -> cp.CoinPayClient:
    global _client
    if _client is None:
        _client = cp.CoinPayClient(cfg)
    return _client


def set_client(client: cp.CoinPayClient | None) -> None:
    """Tests inject a client with a mock transport."""
    global _client
    _client = client


# --- response models (mirror apps/web/lib/backend-api.ts) --------------------


class SubscriptionResponse(BaseModel):
    id: str
    plan_type: str
    agent_limit: int
    current_period_end: str | None
    cancel_at_period_end: bool
    provider: str | None


class UsageResponse(BaseModel):
    total_agents: int
    agent_limit: int
    period_start: str
    period_end: str
    # Additive; the web ignores fields it does not know.
    messages_used: int
    message_limit: int


class CheckoutRequest(BaseModel):
    plan_type: str = Field(default=PRO)
    billing_interval: str | None = None
    success_url: str = ""
    cancel_url: str = ""
    promo_code: str | None = None


class CheckoutResponse(BaseModel):
    checkout_url: str
    session_id: str


class PortalRequest(BaseModel):
    return_url: str = ""


class PortalResponse(BaseModel):
    url: str


# --- helpers -----------------------------------------------------------------


def _sub_response(sub: service.Subscription) -> SubscriptionResponse:
    return SubscriptionResponse(
        id=str(sub.id), plan_type=sub.plan_type, agent_limit=sub.agent_limit,
        current_period_end=iso_z(sub.current_period_end),
        cancel_at_period_end=bool(sub.cancel_at_period_end), provider=sub.provider,
    )


def _same_site(url: str, cfg: CoinPayConfig) -> bool:
    """Only send buyers back to the deployment's own web or backend origin."""
    if not url:
        return True
    try:
        target = urlparse(url)
    except ValueError:
        return False
    allowed = {urlparse(cfg.public_web_url).netloc, urlparse(cfg.public_backend_url).netloc}
    return target.scheme in ("http", "https") and target.netloc in allowed


def _checkout_url(request: Request, cfg: CoinPayConfig, token: str) -> str:
    base = cfg.public_backend_url.rstrip("/") or str(request.base_url).rstrip("/")
    return f"{base}{vicoa_settings.api_v1_prefix}{router.prefix}/coinpay/checkout/{token}"


def _mint_checkout(request: Request, cfg: CoinPayConfig, user: User, plan_type: str, interval: str | None, success_url: str, cancel_url: str) -> CheckoutResponse:
    if not cfg.checkout_enabled:
        raise HTTPException(status_code=503, detail="CoinPay checkout is not configured on this server")
    try:
        q = quote(cfg, plan_type, interval)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not (_same_site(success_url, cfg) and _same_site(cancel_url, cfg)):
        raise HTTPException(status_code=400, detail="success_url / cancel_url must be on this deployment")
    it = intents.new_intent(
        user_id=str(user.id), plan_type=q.plan_type, billing_interval=q.billing_interval,
        success_url=success_url, cancel_url=cancel_url, ttl_seconds=cfg.coinpay_intent_ttl_minutes * 60,
    )
    token = intents.encode(it, cfg.checkout_secret)
    return CheckoutResponse(checkout_url=_checkout_url(request, cfg, token), session_id=it.jti)


# --- the surface Vicoa's clients call ----------------------------------------


@router.get("/subscription", response_model=SubscriptionResponse)
def get_subscription(user: User = Depends(get_current_user), db: Session = Depends(get_db), cfg: CoinPayConfig = Depends(get_config)):
    sub = service.get_or_create_subscription(db, user.id, cfg)
    service.effective(db, sub, cfg=cfg)
    return _sub_response(sub)


@router.get("/usage", response_model=UsageResponse)
def get_usage(user: User = Depends(get_current_user), db: Session = Depends(get_db), cfg: CoinPayConfig = Depends(get_config)):
    sub = service.get_or_create_subscription(db, user.id, cfg)
    service.effective(db, sub, cfg=cfg)
    u = service.usage(db, user.id, sub, cfg=cfg)
    return UsageResponse(
        total_agents=u.total_agents, agent_limit=u.agent_limit, period_start=iso_z(u.period_start) or "",
        period_end=iso_z(u.period_end) or "", messages_used=u.messages_used, message_limit=u.message_limit,
    )


@router.get("/mobile/status")
def get_mobile_status(user: User = Depends(get_current_user), db: Session = Depends(get_db), cfg: CoinPayConfig = Depends(get_config)) -> dict[str, Any]:
    sub = service.get_or_create_subscription(db, user.id, cfg)
    service.effective(db, sub, cfg=cfg)
    return {
        "plan_type": sub.plan_type, "provider": sub.provider, "is_pro": sub.plan_type == PRO,
        "current_period_end": iso_z(sub.current_period_end), "cancel_at_period_end": bool(sub.cancel_at_period_end),
    }


@router.post("/checkout", response_model=CheckoutResponse)
def create_checkout(body: CheckoutRequest, request: Request, user: User = Depends(get_current_user), cfg: CoinPayConfig = Depends(get_config)):
    return _mint_checkout(request, cfg, user, body.plan_type, body.billing_interval, body.success_url, body.cancel_url)


@router.post("/portal", response_model=PortalResponse)
def create_portal(body: PortalRequest, request: Request, user: User = Depends(get_current_user), cfg: CoinPayConfig = Depends(get_config)):
    """There is no card on file to manage. "Manage" means "buy more time", so the
    portal is a fresh monthly checkout that returns to the caller."""
    out = _mint_checkout(request, cfg, user, PRO, "monthly", body.return_url, body.return_url)
    return PortalResponse(url=out.checkout_url)


@router.post("/cancel")
def cancel_subscription(user: User = Depends(get_current_user), db: Session = Depends(get_db), cfg: CoinPayConfig = Depends(get_config)) -> dict[str, Any]:
    sub = service.get_or_create_subscription(db, user.id, cfg)
    service.effective(db, sub, cfg=cfg)
    if sub.plan_type != PRO:
        raise HTTPException(status_code=400, detail="No active Pro pass to cancel")
    service.cancel(db, sub)
    return {"status": "cancelled", "cancel_at_period_end": True, "current_period_end": iso_z(sub.current_period_end),
            "message": "Your pass is prepaid and stays active until it ends. Nothing further will be charged."}


# --- CoinPay specifics --------------------------------------------------------


@router.get("/coinpay/config")
def coinpay_config(cfg: CoinPayConfig = Depends(get_config)) -> dict[str, Any]:
    """Public, secret-free: lets a client (or curl) see what is for sale."""
    return {
        "enabled": cfg.checkout_enabled, "provider": "coinpay",
        "blockchains": [{"code": c, "label": cfg.chain_label(c)} for c in cfg.blockchains],
        "plans": {
            "pro": {
                "monthly": {"amount_usd": cfg.coinpay_pro_monthly_usd, "days": cfg.coinpay_monthly_days},
                "annual": {"amount_usd": cfg.coinpay_pro_annual_usd, "days": cfg.coinpay_annual_days},
            }
        },
        "free_message_limit": cfg.coinpay_free_message_limit,
    }


@router.get("/coinpay/checkout/{token}", response_class=HTMLResponse)
def checkout_page(token: str, request: Request, cfg: CoinPayConfig = Depends(get_config)):
    try:
        it = intents.decode(token, cfg.checkout_secret)
        q = quote(cfg, it.plan_type, it.billing_interval)
    except (intents.IntentError, ValueError) as exc:
        return HTMLResponse(error_page("This checkout link is not valid", str(exc), cfg.public_web_url), status_code=400)
    return HTMLResponse(picker_page(cfg, q, post_url=str(request.url.path), cancel_url=it.cancel_url))


@router.post("/coinpay/checkout/{token}")
def checkout_submit(
    token: str, request: Request, blockchain: str = Form(...), db: Session = Depends(get_db),
    cfg: CoinPayConfig = Depends(get_config), client: cp.CoinPayClient = Depends(get_client),
):
    try:
        it = intents.decode(token, cfg.checkout_secret)
        q = quote(cfg, it.plan_type, it.billing_interval)
    except (intents.IntentError, ValueError) as exc:
        return HTMLResponse(error_page("This checkout link is not valid", str(exc), cfg.public_web_url), status_code=400)
    chain = blockchain.strip().upper()
    if chain not in cfg.blockchains:
        return HTMLResponse(picker_page(cfg, q, post_url=str(request.url.path), cancel_url=it.cancel_url, error="Pick one of the listed networks."), status_code=400)
    try:
        payment = service.start_payment(db, client, cfg, it, q, chain)
    except cp.CoinPayError as exc:
        logger.error("coinpay: create payment failed: %s", exc)
        return HTMLResponse(picker_page(cfg, q, post_url=str(request.url.path), cancel_url=it.cancel_url, error="CoinPay could not start the payment. Try again in a moment."), status_code=502)
    return RedirectResponse(payment.pay_url or client.pay_url(payment.coinpay_payment_id), status_code=303)


@router.post("/coinpay/webhook")
async def coinpay_webhook(request: Request, db: Session = Depends(get_db), cfg: CoinPayConfig = Depends(get_config)):
    if not cfg.coinpay_webhook_secret:
        raise HTTPException(status_code=503, detail="COINPAY_WEBHOOK_SECRET is not configured")
    raw = await request.body()
    if not cp.verify_webhook(raw, request.headers.get("x-coinpay-signature"), cfg.coinpay_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid signature")
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Body is not JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body is not an object")
    payment_id = str(payload.get("payment_id") or payload.get("id") or "")
    if not payment_id:
        raise HTTPException(status_code=400, detail="payment_id missing")
    if cfg.coinpay_business_id and str(payload.get("business_id") or cfg.coinpay_business_id) != cfg.coinpay_business_id:
        raise HTTPException(status_code=400, detail="business_id mismatch")
    row = service.find_payment(db, payment_id)
    if row is None:
        # Genuine webhook for a payment this server never created (another
        # site on the same business, or a manual payment). Acknowledge so
        # CoinPay stops retrying; grant nothing.
        logger.info("coinpay: webhook for unknown payment %s ignored", payment_id)
        return {"received": True, "known": False}
    out = service.apply_status(db, row, event=payload.get("event"), status=payload.get("status"), raw=payload, cfg=cfg)
    return {"received": True, "known": True, "granted": out.granted, "reason": out.reason,
            "current_period_end": iso_z(out.current_period_end)}


@router.get("/coinpay/payments/{coinpay_payment_id}")
def payment_status(
    coinpay_payment_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db),
    cfg: CoinPayConfig = Depends(get_config), client: cp.CoinPayClient = Depends(get_client),
) -> dict[str, Any]:
    """Poll one of your own payments; reconciles against CoinPay on the spot so a
    success page can activate Pro even when the webhook cannot reach this host."""
    row = service.find_payment(db, coinpay_payment_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Payment not found")
    if row.granted_at is None and row.status == "pending":
        try:
            remote = client.get_payment(coinpay_payment_id)
        except cp.CoinPayError as exc:
            logger.warning("coinpay: poll failed: %s", exc)
            remote = None
        if remote is not None:
            service.apply_status(db, row, event=None, status=remote.get("status"), raw=remote, cfg=cfg)
    sub = service.get_or_create_subscription(db, user.id, cfg)
    return {"payment_id": row.coinpay_payment_id, "status": row.status, "granted": row.granted_at is not None,
            "pay_url": row.pay_url, "plan_type": sub.plan_type, "current_period_end": iso_z(sub.current_period_end)}
