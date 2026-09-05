"""The free-plan gate.

The open core has no entitlement hook on the message path, so this is a pure
ASGI middleware that watches ``POST /api/v1/agent-instances/{id}/messages``
(the one route web and mobile use to talk to an agent) and answers 402 when a
free account has used its monthly allowance. Pro passes straight through.

It fails OPEN: any error resolving the user or counting messages lets the
request continue. Blocking a paying customer because a query timed out is a
far worse outcome than one free message too many.
"""

from __future__ import annotations

import json
import logging
import re
from uuid import UUID

from shared.auth.provider import verify_user_token
from shared.database.session import SessionLocal

from . import service
from .config import get_config

logger = logging.getLogger(__name__)

GATED = re.compile(r"^/api/v1/agent-instances/[^/]+/messages/?$")


def _bearer(scope) -> str | None:
    for name, value in scope.get("headers") or []:
        if name == b"authorization":
            text = value.decode("latin-1")
            if text.lower().startswith("bearer "):
                return text[7:].strip()
    return None


def check(token: str | None) -> tuple[bool, int, int]:
    """(blocked, used, limit). Runs a DB session; called in a thread."""
    if not token:
        return False, 0, -1
    principal = verify_user_token(token)
    with SessionLocal() as db:
        return service.over_free_limit(db, UUID(str(principal.user_id)), get_config())


class FreePlanGate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST" or not GATED.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        cfg = get_config()
        if cfg.coinpay_free_message_limit <= 0:
            await self.app(scope, receive, send)
            return
        try:
            import asyncio

            blocked, used, limit = await asyncio.to_thread(check, _bearer(scope))
        except Exception:  # fail open, see module docstring
            logger.exception("coinpay gate: could not evaluate entitlement; allowing request")
            blocked, used, limit = False, 0, -1
        if not blocked:
            await self.app(scope, receive, send)
            return
        body = json.dumps({
            "detail": f"Free plan limit reached ({used}/{limit} messages this month). Upgrade to Pro to keep going.",
            "code": "free_plan_limit", "used": used, "limit": limit,
            "upgrade_url": f"{cfg.public_web_url.rstrip('/')}/dashboard/upgrade",
        }).encode()
        await send({"type": "http.response.start", "status": 402, "headers": [
            (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
        ]})
        await send({"type": "http.response.body", "body": body})
