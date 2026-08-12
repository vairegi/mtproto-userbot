"""
popup.py — GET /api/popup + POST /api/popup/ack  (v12.3)

Serves the admin-configurable popup shown on mini-app open. All state lives
in the `control_flags` collection (same store /popupon /popupoff etc. use):

    popup_enabled          "1" | "0"
    popup_message          free text
    popup_image_file_id    Telegram file_id (photo attached to /popupmsg)
    popup_freq_hours       int, default 2

Per-user throttle in the `popup_views` collection:
    _id = user_id (int), last_shown = UTC datetime.

The popup image itself is served by GET /api/popup/image, which streams the
Telegram-hosted photo (file_id → Bot API getFile → file download) through
the mini-app backend so the frontend never needs the bot token.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, Response

from ..auth import get_current_user

import db as _db

router = APIRouter(prefix="/api/popup", tags=["popup"])

_DEFAULT_FREQ_HOURS = 2

# In-memory cache for the resolved Telegram file path (file_path is valid
# for ~1 hour). Keyed by file_id; value = (expiry_epoch, tg_file_path).
_tg_path_cache: dict[str, tuple[float, str]] = {}


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _bot_token() -> str:
    # Bot 1 (admin bot) owns the photos admins attach to /popupmsg.
    return os.environ.get("BOT1_TOKEN", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")


@router.get("")
def get_popup(user: dict = Depends(get_current_user)) -> dict:
    """Return the popup payload for THIS user, honouring the throttle window.

    Response shape:
      { "show": bool, "message": str, "has_image": bool, "freq_hours": int }
    show=False means the frontend must not render the modal at all.
    """
    conn = _db.connect()
    try:
        enabled = _db.get_flag(conn, "popup_enabled", "0") == "1"
        if not enabled:
            return {"show": False, "message": "", "has_image": False, "freq_hours": _DEFAULT_FREQ_HOURS}

        message = _db.get_flag(conn, "popup_message", "")
        image_file_id = _db.get_flag(conn, "popup_image_file_id", "")
        try:
            freq_hours = int(_db.get_flag(conn, "popup_freq_hours", str(_DEFAULT_FREQ_HOURS)))
            if freq_hours < 0:
                freq_hours = _DEFAULT_FREQ_HOURS
        except (TypeError, ValueError):
            freq_hours = _DEFAULT_FREQ_HOURS

        # Nothing configured → nothing to show.
        if not message.strip() and not image_file_id.strip():
            return {"show": False, "message": "", "has_image": False, "freq_hours": freq_hours}

        uid = int(user.get("id") or user.get("user_id") or 0)
        if uid and freq_hours > 0:
            try:
                doc = conn.db["popup_views"].find_one({"_id": uid})
                if doc and doc.get("last_shown"):
                    last = doc["last_shown"]
                    if isinstance(last, datetime):
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                        if _now() - last < timedelta(hours=freq_hours):
                            return {"show": False, "message": "", "has_image": False, "freq_hours": freq_hours}
            except Exception:  # noqa: BLE001
                pass  # throttle lookup failed → fail open, show the popup

        return {
            "show": True,
            "message": message,
            "has_image": bool(image_file_id.strip()),
            "freq_hours": freq_hours,
        }
    finally:
        conn.close()


@router.post("/ack")
def ack_popup(user: dict = Depends(get_current_user)) -> dict:
    """Record that this user just saw / dismissed the popup so the throttle
    window resets from this moment. Called by the frontend when the popup
    is opened AND when the × is tapped (idempotent)."""
    uid = int(user.get("id") or user.get("user_id") or 0)
    if not uid:
        return {"ok": True}
    conn = _db.connect()
    try:
        conn.db["popup_views"].update_one(
            {"_id": uid},
            {"$set": {"last_shown": _now()}},
            upsert=True,
        )
        return {"ok": True}
    except Exception:  # noqa: BLE001
        return {"ok": True}  # never block the UI on a stats write
    finally:
        conn.close()


@router.get("/image")
def popup_image(user: dict = Depends(get_current_user)) -> Response:
    """Stream the admin-attached popup image from Telegram through the backend.

    Why proxy instead of exposing the file_id: the mini-app frontend has no
    bot token, and Telegram file URLs require one. We resolve file_id →
    file_path via getFile (cached for 50 min) then stream the bytes.
    """
    conn = _db.connect()
    try:
        file_id = _db.get_flag(conn, "popup_image_file_id", "").strip()
    finally:
        conn.close()
    if not file_id:
        return Response(status_code=404)
    token = _bot_token()
    if not token:
        return Response(status_code=503, content=b"bot token not configured")

    # Resolve file_id → file_path (cached).
    now = __import__("time").time()
    cached = _tg_path_cache.get(file_id)
    if cached and cached[0] > now:
        file_path = cached[1]
    else:
        try:
            r = httpx.get(
                f"https://api.telegram.org/bot{token}/getFile",
                params={"file_id": file_id}, timeout=10.0,
            )
            data = r.json()
            file_path = (data.get("result") or {}).get("file_path") or ""
        except Exception:  # noqa: BLE001
            return Response(status_code=502, content=b"telegram getFile failed")
        if not file_path:
            return Response(status_code=404)
        _tg_path_cache[file_id] = (now + 3000, file_path)  # ~50 min

    try:
        img = httpx.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}",
            timeout=20.0,
        )
        if img.status_code != 200:
            return Response(status_code=502)
        ctype = img.headers.get("content-type", "image/jpeg")
        # Aggressive client caching: the admin changes the image rarely.
        return Response(
            content=img.content,
            media_type=ctype,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    except Exception:  # noqa: BLE001
        return Response(status_code=502)
