"""
queue.py — /api/queue (POST), /api/queue/status (GET),
           /api/queue/deliver/{gallery_id} (POST — BUG 1 fix)

POST enqueues a URL into the SAME MongoDB job queue admin_bot.py polls, so
worker.py picks it up seamlessly. GET returns live counts + recent jobs
for the frontend header badge + Queue tab.

Rate limiting happens BEFORE enqueue via ratelimit.check_and_consume.

BUG 1 fix — dedup delivery:
When the dedup gate says a gallery is `already_completed`, instead of
returning an `open_link` for the frontend to jump to (which lands the user
in the channel), we now use the admin bot's Bot API `copyMessage` to
forward the cover + PDF straight into the user's DM.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db, ratelimit
from ..auth import get_current_user
from ..config import settings
from ..services import dm_delivery, queue_bridge

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
        # BUG 1 fix — DM the user the cover + PDF directly instead of
        # returning open_link (which would jump them to the channel).
        gid = peek.get("gallery_id")
        deliv = dm_delivery.deliver_to_dm(gid, uid) if gid else {
            "ok": False, "delivered": False, "reason": "no gallery_id"
        }
        out = {
            "ok": True,
            "deduped": True,
            "action": "already_completed",
            "gallery_id": gid,
            "status": peek.get("status"),
            "title": peek.get("title"),
            "delivered": bool(deliv.get("delivered")),
            "usage": ratelimit.usage_summary(uid),
        }
        if deliv.get("delivered"):
            out["message"] = "📨 Forwarded to your DM"
        else:
            # Delivery failed — surface the reason but still mark it as a
            # dedup hit; the frontend can toast the error instead of
            # opening the channel link.
            out["message"] = "Already in the library, but DM delivery failed."
            out["delivery_error"] = deliv.get("reason") or deliv.get("cover_error") \
                or deliv.get("pdf_error") or "unknown"
        return out

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


@router.post("/deliver/{gallery_id}")
def deliver(gallery_id: str, user: dict = Depends(get_current_user)) -> dict:
    """BUG 1 fix — forward the cover + PDF from the database channel into
    the requester's DM using the admin bot's copyMessage endpoint."""
    uid = int(user["id"])
    # If app is private, still block non-admins (same rule as enqueue).
    if not db.get_public_mode() and uid != int(settings.admin_user_id):
        raise HTTPException(403, "App is currently private (admin only).")

    res = dm_delivery.deliver_to_dm(gallery_id, uid)
    if not res.get("ok"):
        # 404 when the gallery isn't in the library; 502 when Telegram
        # itself refused the copy; 500 for anything else.
        reason = res.get("reason") or ""
        if "not found" in reason.lower():
            raise HTTPException(404, reason)
        if "not configured" in reason.lower():
            raise HTTPException(500, reason)
        if reason:
            raise HTTPException(502, reason)
        raise HTTPException(500, "DM delivery failed")
    return {"ok": True, "delivered": True, "gallery_id": res.get("gallery_id")}


@router.get("/status")
def status(_user: dict = Depends(get_current_user)) -> dict:
    return queue_bridge.status_summary()
