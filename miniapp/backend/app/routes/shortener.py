"""
shortener.py — v13.0 link-shortener verification gate routes (Bot 0).

Auto-mounted by routes/__init__.py (no main.py edit needed).

    GET /api/shortener/status   → passive state; prompt=true only on actions
    GET /api/shortener/link     → mint a token + return the shortened URL
    GET /api/shortener/verify   → browser fallback landing (deep-link is the
                                  primary path via /start verify_<token>)

v13.16: no direct-token fallback. Protected actions remain locked on error.
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException
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
        log.warning("shortener: getMe failed (%s)", type(e).__name__)
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

    Fired only by an explicit action prompt, never passive polling. Throttled. Best-effort:
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
        short = svc.issue_link(uid, uname)
        rows = [[{"text": svc.UNLOCK_BUTTON_LABEL, "url": short}]]
        for b in svc.extra_buttons():
            rows.append([{"text": b["label"], "url": b["url"]}])
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": int(uid),
                "text": svc.bot_message(),
                "reply_markup": {"inline_keyboard": rows},
            },
            timeout=10.0,
        )
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram rejected DM")
        log.info("shortener: verify DM sent to %s", uid)
    except Exception as e:
        _last_dm.pop(uid, None)  # failed sends must not suppress retries
        log.warning("shortener: verify DM failed (%s)", type(e).__name__)


@router.get("/status")
def shortener_status(prompt: bool = False, user: dict = Depends(get_current_user)) -> dict:
    """Passive status by default; prompt=true sends a throttled DM only
    after an explicit Read / Download action. Polling never sends DMs."""
    uid = int(user.get("id") or 0)
    payload = svc.status_for(uid, settings.admin_user_id)
    if prompt and payload.get("locked"):
        _maybe_send_verify_dm(uid)
    return payload


@router.get("/link")
def shortener_link(user: dict = Depends(get_current_user)) -> dict:
    """Only the provider-issued short URL is public. Never expose deep_link."""
    uid = int(user.get("id") or 0)
    if svc.is_verified(uid, settings.admin_user_id):
        return {"ok": True, "url": "", "verified": True}
    try:
        short = svc.issue_link(uid, _bot_username())
        return {"ok": True, "url": short}
    except svc.ProviderUnavailable as exc:
        raise HTTPException(503, detail={"code": "shortener_unavailable",
                                        "message": str(exc)}) from None
    except Exception:
        raise HTTPException(503, detail={"code": "shortener_unavailable",
            "message": "Verification is unavailable. Access remains locked; please retry later."}) from None


@router.get("/verify", response_class=HTMLResponse)
def shortener_verify(token: str = "") -> HTMLResponse:
    """Legacy browser endpoint is informational only; it cannot unlock users."""
    body = ("<h1>Verification must finish in Telegram</h1>"
            "<p>Use a fresh verification link from Read or Download. "
            "This page cannot grant access.</p>")
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Verification</title>"
        "<style>body{font-family:system-ui;background:#0f1420;color:#e7ecf5;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "margin:0;text-align:center;padding:24px}</style></head>"
        f"<body><div>{body}</div></body></html>"
    )
