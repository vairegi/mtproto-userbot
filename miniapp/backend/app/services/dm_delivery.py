"""
dm_delivery.py — BUG 1 fix.

When a user taps "Queue" on a gallery that is already in the database
channel, we must NOT open a t.me/c/<internal>/<msg> link (which just jumps
them to the channel). Instead, the admin bot copyMessage's the cover post
+ PDF from the database channel directly into the user's DM.

copyMessage is preferred over forwardMessage because it strips the
"Forwarded from" header cleanly.

This module exposes a single sync function `deliver_to_dm(gallery_id, user_id)`
that:
  1. Looks up the gallery in Mongo (`galleries[gid]`) via gallery_state.
  2. Extracts db_cover_msg_id + db_pdf_msg_id + the DB channel id.
  3. Calls Bot-API copyMessage twice with the admin bot token.
  4. Returns {ok, delivered, ...}.

The parent bot / worker.py already writes db_cover_msg_id and db_pdf_msg_id
onto the gallery doc when the cover post + PDF land in the DB channel.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional

import httpx

from ..config import settings

log = logging.getLogger("miniapp.dm_delivery")

# --- Make parent-bot helpers importable (same trick as queue_bridge) --------
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in [
    os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..")),
    "/opt/render/project/src",
]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import db as _bot_db            # type: ignore
    import gallery_state as _gs     # type: ignore
    HAVE_GS = True
except Exception as e:  # noqa: BLE001
    _bot_db = None
    _gs = None
    HAVE_GS = False
    log.warning("gallery_state/db not importable — DM delivery disabled (%s)", e)


_TG_API = "https://api.telegram.org"
_TIMEOUT = 15.0


def _bot_token() -> str:
    """Resolve the admin bot token from env / settings."""
    return (
        settings.bot_token
        or os.environ.get("BOT_TOKEN", "")
        or os.environ.get("ADMIN_BOT_TOKEN", "")
    )


def _channel_id() -> int:
    """Resolve the database channel id from env / settings."""
    try:
        cid = int(getattr(settings, "database_channel_id", 0) or 0)
    except (TypeError, ValueError):
        cid = 0
    if cid:
        return cid
    for name in ("DATABASE_CHANNEL_ID", "CHANNEL_ID"):
        v = os.environ.get(name)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return 0


def _copy_one(
    token: str,
    from_chat_id: int,
    chat_id: int,
    message_id: int,
    client: httpx.Client,
) -> dict:
    """Call Telegram Bot API `copyMessage` once. Returns the JSON envelope
    unchanged so callers can inspect .ok / .description."""
    url = f"{_TG_API}/bot{token}/copyMessage"
    r = client.post(
        url,
        json={
            "chat_id":       int(chat_id),
            "from_chat_id":  int(from_chat_id),
            "message_id":    int(message_id),
        },
        timeout=_TIMEOUT,
    )
    try:
        return r.json() or {}
    except Exception:
        return {"ok": False, "description": f"non-JSON response HTTP {r.status_code}"}


def deliver_to_dm(gallery_id: str, user_id: int) -> dict:
    """Copy the cover + PDF for `gallery_id` from the DB channel into
    the user's DM. Returns a dict describing what happened. Never raises
    on Telegram-side errors — surfaces them in the dict so the caller can
    decide (fall back to open_link, toast a message, etc.).
    """
    if not HAVE_GS:
        return {"ok": False, "delivered": False, "reason": "gallery_state unavailable"}

    gid = str(gallery_id or "").strip().lstrip("#")
    if not gid:
        return {"ok": False, "delivered": False, "reason": "missing gallery_id"}

    token = _bot_token()
    if not token:
        return {"ok": False, "delivered": False, "reason": "BOT_TOKEN not configured"}

    from_chat = _channel_id()
    if not from_chat:
        return {"ok": False, "delivered": False, "reason": "DATABASE_CHANNEL_ID not configured"}

    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return {"ok": False, "delivered": False, "reason": "bad user_id"}

    # --- Look up the gallery doc --------------------------------------------
    conn = _bot_db.connect()
    try:
        doc = _gs.get(conn, gid) or {}
    finally:
        try:
            conn.close()
        except Exception:
            pass

    if not doc:
        return {"ok": False, "delivered": False, "reason": "gallery not found", "gallery_id": gid}

    cover_msg_id = doc.get("db_cover_msg_id")
    pdf_msg_id   = doc.get("db_pdf_msg_id")

    if not cover_msg_id and not pdf_msg_id:
        return {
            "ok": False,
            "delivered": False,
            "reason": "gallery has no stored DB channel message IDs",
            "gallery_id": gid,
        }

    # --- Fire copyMessage x2 -----------------------------------------------
    result: dict[str, Any] = {
        "ok": True,
        "delivered": False,
        "gallery_id": gid,
        "cover_copied": False,
        "pdf_copied": False,
    }
    with httpx.Client() as client:
        if cover_msg_id:
            r = _copy_one(token, from_chat, uid, int(cover_msg_id), client)
            result["cover_copied"] = bool(r.get("ok"))
            if not r.get("ok"):
                result["cover_error"] = r.get("description") or "unknown"
                log.warning("cover copyMessage failed for gid=%s uid=%s: %s",
                            gid, uid, result["cover_error"])
        if pdf_msg_id:
            r = _copy_one(token, from_chat, uid, int(pdf_msg_id), client)
            result["pdf_copied"] = bool(r.get("ok"))
            if not r.get("ok"):
                result["pdf_error"] = r.get("description") or "unknown"
                log.warning("pdf copyMessage failed for gid=%s uid=%s: %s",
                            gid, uid, result["pdf_error"])

    result["delivered"] = result["cover_copied"] or result["pdf_copied"]
    if not result["delivered"]:
        result["ok"] = False
    return result
