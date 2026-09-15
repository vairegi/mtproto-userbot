"""
shortener.py — v13.0 link-shortener verification gate routes (Bot 0).

Auto-mounted by routes/__init__.py (no main.py edit needed).

    GET /api/shortener/status   → gate state for THIS user (polled on boot)
    GET /api/shortener/link     → mint a token + return the shortened URL
    GET /api/shortener/verify   → browser fallback landing (deep-link is the
                                  primary path via /start verify_<token>)

The gate fails open everywhere (see services/shortener.py).
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse

from ..auth import get_current_user
from ..config import settings
from ..services import shortener as svc

log = logging.getLogger("miniapp.routes.shortener")

router = APIRouter(prefix="/api/shortener", tags=["shortener"])

# Resolved once per process; used to build the t.me deep link.
_bot_username_cache: str = ""


def _bot_username() -> str:
    """Bot 0's public username — read live from getMe, cached per process.

    Falls back to BOT_USERNAME env if Telegram is unreachable (deep link
    then just can't be built — we return it empty and fail open).
    """
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    env = os.environ.get("BOT_USERNAME", "").lstrip("@").strip()
    token = settings.bot_token or os.environ.get("BOT_TOKEN", "") \
        or os.environ.get("ADMIN_BOT_TOKEN", "")
    if not token:
        return env
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=8.0)
        uname = (r.json().get("result") or {}).get("username", "")
        if uname:
            _bot_username_cache = uname
            return uname
    except Exception as e:  # noqa: BLE001
        log.warning("shortener: getMe failed (%s)", e)
    return env


# Throttle for the "we sent you a DM" nudge so a user sitting on the
# overlay (status is polled every few seconds) gets ONE DM per lock window,
# not one per poll. In-memory per-process is fine (worst case after a
# restart: one extra DM).
_DM_THROTTLE_S = 30 * 60
_last_dm: dict[int, float] = {}


def _bot_token() -> str:
    return settings.bot_token or os.environ.get("BOT_TOKEN", "") \
        or os.environ.get("ADMIN_BOT_TOKEN", "")


def _maybe_send_verify_dm(uid: int) -> None:
    """DM the user the verification message + buttons (bot DM text).

    Fired when a LOCKED user polls status. Throttled per user. Best-effort:
    any failure is logged and swallowed — the overlay already tells the user
    to expect the DM, and the overlay's own button is a working fallback.
    """
    now = time.time()
    if now - _last_dm.get(uid, 0) < _DM_THROTTLE_S:
        return
    token = _bot_token()
    uname = _bot_username()
    if not token or not uname:
        return
    _last_dm[uid] = now  # mark first so a slow send can't double-fire
    try:
        vtoken = svc.mint_token(uid)
        deep_link = f"https://t.me/{uname}?start=verify_{vtoken}"
        short = svc.shorten(deep_link)
        rows = [[{"text": svc.UNLOCK_BUTTON_LABEL, "url": short}]]
        for b in svc.extra_buttons():
            rows.append([{"text": b["label"], "url": b["url"]}])
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": int(uid),
                "text": svc.bot_message(),
                "reply_markup": {"inline_keyboard": rows},
            },
            timeout=10.0,
        )
        log.info("shortener: verify DM sent to %s", uid)
    except Exception as e:  # noqa: BLE001
        log.warning("shortener: verify DM to %s failed: %s", uid, e)


@router.get("/status")
def shortener_status(user: dict = Depends(get_current_user)) -> dict:
    """Gate state for the calling user. The frontend polls this on boot and
    re-polls while the overlay is up so it auto-clears after verification."""
    uid = int(user.get("id") or 0)
    payload = svc.status_for(uid, settings.admin_user_id)
    if payload.get("locked"):
        _maybe_send_verify_dm(uid)
    return payload


@router.get("/link")
def shortener_link(user: dict = Depends(get_current_user)) -> dict:
    """Mint a one-time verify token for this user and return the shortened
    URL that ultimately deep-links back to the bot (/start verify_<token>).

    Fail-open: if the provider is down the raw deep link is returned so the
    user can still verify directly.
    """
    uid = int(user.get("id") or 0)
    uname = _bot_username()
    if not uname:
        # Cannot build a deep link at all — treat as verified (fail open).
        return {"ok": True, "url": "", "deep_link": "", "fail_open": True}
    token = svc.mint_token(uid)
    deep_link = f"https://t.me/{uname}?start=verify_{token}"
    short = svc.shorten(deep_link)
    return {"ok": True, "url": short, "deep_link": deep_link, "fail_open": False}


@router.get("/verify", response_class=HTMLResponse)
def shortener_verify(token: str = "") -> HTMLResponse:
    """Browser fallback landing page. The primary verification path is the
    Telegram deep link (/start verify_<token> handled in admin_bot.py); this
    page exists so a token opened in a plain browser still completes and the
    user gets a friendly confirmation."""
    uid = svc.verify_token(token) if token else None
    if uid:
        body = (
            "<h1>✅ Verified</h1>"
            "<p>Access unlocked. Return to the mini app — it will open "
            "automatically.</p>"
        )
    else:
        body = (
            "<h1>⚠️ Invalid or expired link</h1>"
            "<p>Please request a fresh verification link from the mini app "
            "or the bot.</p>"
        )
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Verification</title>"
        "<style>body{font-family:system-ui;background:#0f1420;color:#e7ecf5;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "margin:0;text-align:center;padding:24px}</style></head>"
        f"<body><div>{body}</div></body></html>"
    )
