"""telegram.py — /telegram webhook endpoint for BOT 1's admin chat commands.

The webhook is protected by an optional `?s=<BOT1_WEBHOOK_SECRET>` query
string set at setWebhook time. Telegram will always call the exact URL
you registered, so a random secret is enough to keep drive-by scanners
out.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import settings
from ..services import telegram_bot

log = logging.getLogger("scraperbot.telegram_route")

router = APIRouter()


@router.post("/telegram")
async def telegram_webhook(request: Request, s: str = Query(default="")) -> dict:
    if settings.webhook_secret:
        if (s or "").strip() != settings.webhook_secret:
            raise HTTPException(status_code=401, detail="bad webhook secret")
    try:
        update = await request.json()
    except Exception as e:  # noqa: BLE001
        log.warning("telegram webhook: bad JSON: %s", e)
        return {"ok": True}   # ack so Telegram doesn't retry a poison msg
    try:
        await telegram_bot.handle_update(update)
    except Exception as e:  # noqa: BLE001
        log.exception("telegram webhook handler failed: %s", e)
    return {"ok": True}
