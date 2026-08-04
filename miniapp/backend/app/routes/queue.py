"""
queue.py — /api/queue (POST) + /api/queue/status (GET)

POST enqueues a URL into the SAME MongoDB job queue admin_bot.py polls, so
worker.py picks it up seamlessly. GET returns live counts + recent jobs
for the frontend header badge + Queue tab.

Rate limiting happens BEFORE enqueue via ratelimit.check_and_consume.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db, ratelimit
from ..auth import get_current_user
from ..config import settings
from ..services import queue_bridge

router = APIRouter(prefix="/api/queue", tags=["queue"])


class EnqueueBody(BaseModel):
    url: str


@router.post("")
def enqueue(body: EnqueueBody, user: dict = Depends(get_current_user)) -> dict:
    uid = int(user["id"])

    # If app is private, block non-admins.
    if not db.get_public_mode() and not settings.is_admin(uid):  # v0.3
        raise HTTPException(403, "App is currently private (admin only).")

    # Rate-limit check + consume (raises 429 if over limit).
    rl = ratelimit.check_and_consume(uid)

    # Actually enqueue into the bot's shared queue.
    try:
        r = queue_bridge.enqueue(body.url, uid, user.get("username"))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Enqueue failed: {e}")

    return {"ok": True, "job": r, "usage": rl}


@router.get("/status")
def status(_user: dict = Depends(get_current_user)) -> dict:
    return queue_bridge.status_summary()
