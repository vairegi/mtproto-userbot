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
    if not db.get_public_mode() and uid != int(settings.admin_user_id):
        raise HTTPException(403, "App is currently private (admin only).")

    # ---- V2 dedup gate -----------------------------------------------------
    # Runs BEFORE the rate-limit consume on purpose: tapping "Queue" on a
    # gallery we already have must not cost the user one of their daily
    # tokens, and must not create a junk queue row. This is a read-only
    # peek — relay_v2 remains the single writer of the PROCESSING claim.
    try:
        peek = queue_bridge.dedup_peek(body.url)
    except Exception as e:  # noqa: BLE001
        # A broken dedup gate must never block queueing.
        peek = {"verdict": "proceed", "peek_error": str(e)}

    verdict = peek.get("verdict")

    if verdict == "already_completed":
        return {
            "ok": True,
            "deduped": True,
            "action": "already_completed",
            "gallery_id": peek.get("gallery_id"),
            "status": peek.get("status"),
            "open_link": peek.get("open_link"),
            "title": peek.get("title"),
            "message": "Already in the library — opening the existing post.",
            "usage": ratelimit.usage_summary(uid),
        }

    if verdict == "already_processing":
        return {
            "ok": True,
            "deduped": True,
            "action": "already_processing",
            "gallery_id": peek.get("gallery_id"),
            "status": peek.get("status"),
            "title": peek.get("title"),
            "message": "This one is already downloading — hang tight.",
            "usage": ratelimit.usage_summary(uid),
        }

    # ---- Normal path: rate-limit check + consume (raises 429 if over) ------
    rl = ratelimit.check_and_consume(uid)

    # Actually enqueue into the bot's shared queue.
    try:
        r = queue_bridge.enqueue(body.url, uid, user.get("username"))
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Enqueue failed: {e}")

    out = {"ok": True, "deduped": False, "action": "queued", "job": r, "usage": rl}
    # Surface a retry-after-failure hint so the UI can say "retrying…".
    if peek.get("previous_status"):
        out["previous_status"] = peek["previous_status"]
        out["previous_reason"] = peek.get("previous_reason") or ""
    return out


@router.get("/status")
def status(_user: dict = Depends(get_current_user)) -> dict:
    return queue_bridge.status_summary()
