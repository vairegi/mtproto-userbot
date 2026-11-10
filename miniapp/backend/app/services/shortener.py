"""
shortener.py — v13.0 link-shortener verification gate (Bot 0 only).

Single source of truth for the verification state shared by:
  - mini-app backend  (routes/shortener.py — status/link endpoints)
  - admin_bot.py      (/shortener* commands + /start verify_<token>)
  - queue route       (Download gate)

All config lives in `control_flags` (string store, same as popup flags):

    shortener_enabled        "1" | "0"                     (/shortener on|off)
    shortener_api_url        full VPLINK api base, e.g.
                             "https://vplink.in/api?api=TOKEN&url="  (/shortenerapi)
    shortener_hours          unlock TTL hours, default 6             (/setverifytime)
    shortener_app_msg        mini-app overlay text                   (/shortenermsg)
    shortener_bot_msg        bot DM text                             (/shortenerbotmsg)
    shortener_verify_msg     post-verify success text (bot, /verifymsg)
    shortener_buttons        JSON list [{"label","url"}]             (/shortenerbtn)

Per-user state lives in two Mongo collections (best-effort writes,
epoch seconds — floats, per the shared §5.5 contract):

    shortener_tokens   {_id: token, uid, created, used}
    shortener_unlocks  {_id: uid, unlocked_until: float, visits_today: int,
                        day: "YYYY-MM-DD", visits_total: int}

v13.16: protected actions fail closed on provider/config/database failure.
Browsing is always allowed. Explicit OFF and the configured admin still bypass.
Only successfully issued version-2 provider tokens can unlock; legacy grants
and tokens are intentionally not trusted after this security fix.
"""
from __future__ import annotations

import json
import logging

import secrets
from urllib.parse import urlsplit, parse_qsl, urlencode, urlunsplit
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from ..rootdb import load as _load_root_db

log = logging.getLogger("miniapp.shortener")

_db = _load_root_db()

# ---------------------------------------------------------------------------
# Defaults (out-of-the-box, per spec)
# ---------------------------------------------------------------------------
DEFAULT_HOURS = 6            # unlock TTL hours
DEFAULT_APP_MSG = (
    "⚠️ Verification Required: We have sent a verification link to your "
    "Telegram DM. Please complete it to unlock full access."
)
DEFAULT_BOT_MSG = (
    "🔐 Verification required. Tap the button below and complete the short "
    "link to unlock full access to the Universe."
)
UNLOCK_BUTTON_LABEL = "🔓 Verify & Unlock"

# How long a minted token stays valid (seconds) — generous, links are shared.
TOKEN_TTL = 24 * 3600


def _now() -> float:
    return time.time()


def _today() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Flag I/O (control_flags, same store admin_bot writes)
# ---------------------------------------------------------------------------
def _flag(key: str, default: str = "") -> str:
    conn = _db.connect()
    try:
        return _db.get_flag(conn, key, default)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def is_enabled() -> bool:
    return _flag("shortener_enabled", "0") == "1"


def api_url() -> str:
    return _flag("shortener_api_url", "").strip()


def ttl_hours() -> int:
    try:
        n = int(_flag("shortener_hours", str(DEFAULT_HOURS)))
        return n if n > 0 else DEFAULT_HOURS
    except (TypeError, ValueError):
        return DEFAULT_HOURS


def app_message() -> str:
    return _flag("shortener_app_msg", DEFAULT_APP_MSG) or DEFAULT_APP_MSG


def bot_message() -> str:
    return _flag("shortener_bot_msg", DEFAULT_BOT_MSG) or DEFAULT_BOT_MSG


def extra_buttons() -> List[Dict[str, str]]:
    raw = _flag("shortener_buttons", "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, list):
        return []
    out: List[Dict[str, str]] = []
    for b in data:
        if isinstance(b, dict) and b.get("label") and b.get("url"):
            out.append({"label": str(b["label"]), "url": str(b["url"])})
    return out


# ---------------------------------------------------------------------------
# Mongo collections
# ---------------------------------------------------------------------------
def _tokens():
    conn = _db.connect()
    return conn, conn.db["shortener_tokens"]


def _unlocks():
    conn = _db.connect()
    return conn, conn.db["shortener_unlocks"]


def _is_admin(uid: int, admin_user_id: int) -> bool:
    try:
        return admin_user_id and int(uid) == int(admin_user_id)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------
def is_verified(uid: int, admin_user_id: int) -> bool:
    """No error/config path grants an unlock. Browsing does not call this gate."""
    if _is_admin(uid, admin_user_id):
        return True
    try:
        if not is_enabled():
            return True
        conn, col = _unlocks()
        try:
            doc = col.find_one({"_id": int(uid)})
        finally:
            conn.close()
        return bool(doc and doc.get("gate_version") == 2
                    and _now() < float(doc.get("unlocked_until", 0) or 0))
    except Exception:
        return False


def status_for(uid: int, admin_user_id: int) -> Dict[str, Any]:
    """Payload for GET /api/shortener/status."""
    verified = is_verified(uid, admin_user_id)
    try:
        enabled = is_enabled()
        msg, buttons, hours = app_message(), extra_buttons(), ttl_hours()
        configured = not enabled or valid_api_url(api_url())
    except Exception:
        enabled, configured = True, False
        msg, buttons, hours = DEFAULT_APP_MSG, [], DEFAULT_HOURS
    return {
        "enabled": enabled, "verified": verified,
        "locked": enabled and not verified,
        "message": msg, "buttons": buttons,
        "unlock_button": UNLOCK_BUTTON_LABEL, "hours": hours,
        "configuration_error": enabled and not configured,
    }


# ---------------------------------------------------------------------------
# Token mint / verify
# ---------------------------------------------------------------------------
class ProviderUnavailable(RuntimeError):
    """Safe public error; never contains the API key or destination token."""


def valid_api_url(base: str) -> bool:
    try:
        u = urlsplit(base)
        return bool(u.scheme in ("http", "https") and u.hostname
                    and not u.username and not u.password
                    and not any(c.isspace() for c in base))
    except (ValueError, TypeError):
        return False


def mint_token(uid: int) -> str:
    token = secrets.token_urlsafe(24)
    conn, col = _tokens()
    try:
        # Unissued tokens are never redeemable. No best-effort success.
        col.insert_one({"_id": token, "uid": int(uid), "created": _now(),
                        "used": False, "issued": False, "gate_version": 2})
    finally:
        conn.close()
    return token


def shorten(deep_link: str) -> str:
    """Return ONLY a real short URL; never fall back to the redeem token."""
    try:
        base = api_url()
        if not valid_api_url(base):
            raise ProviderUnavailable("Shortener configuration is invalid. Contact the admin.")
        u = urlsplit(base)
        query = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
                 if k.lower() != "url"]
        query.append(("url", deep_link))
        target = urlunsplit((u.scheme, u.netloc, u.path, urlencode(query), ""))
        r = httpx.get(target, timeout=10.0, follow_redirects=True)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError("response type")
        if str(data.get("status", "success")).lower() not in ("success", "ok", "1", "true"):
            raise ValueError("provider rejection")
        short = (data.get("shortenedUrl") or data.get("shortened_url")
                 or data.get("short") or data.get("url") or "")
        if not isinstance(short, str) or not valid_api_url(short):
            raise ValueError("missing short url")
        parsed = urlsplit(short)
        # Reject direct Telegram destinations or a response exposing our
        # bearer token anywhere (including encoded query parameters).
        from urllib.parse import unquote
        decoded = unquote(unquote(short))
        raw_token = deep_link.rsplit("verify_", 1)[-1]
        if (parsed.hostname.lower() in ("t.me", "telegram.me", "telegram.dog")
                or short == deep_link or (raw_token and raw_token in decoded)):
            raise ValueError("unsafe destination")
        return short
    except ProviderUnavailable:
        raise
    except Exception as exc:
        log.warning("shortener: provider unavailable (%s); access remains locked",
                    type(exc).__name__)
        raise ProviderUnavailable("Verification link unavailable. Please retry later; access remains locked.") from None


def issue_link(uid: int, bot_username: str) -> str:
    """Private redeem token goes only to provider; caller sees only short URL."""
    if not bot_username:
        raise ProviderUnavailable("Verification bot is unavailable. Please retry later.")
    token = mint_token(uid)
    short = shorten(f"https://t.me/{bot_username}?start=verify_{token}")
    conn, col = _tokens()
    try:
        result = col.update_one({"_id": token, "uid": int(uid), "used": False,
                                 "gate_version": 2}, {"$set": {"issued": True}})
        if result.modified_count != 1:
            raise ProviderUnavailable("Could not prepare verification. Please retry.")
    finally:
        conn.close()
    return short


def verify_token(token: str, uid: Optional[int] = None) -> Optional[int]:
    """Atomic, user-bound redemption; never trusts legacy/unissued/replayed links.

    A valid provider-issued return token is the completion signal. A generic
    shortener does not provide cryptographic proof of ad/captcha completion.
    """
    if not token or uid is None:
        return None
    from pymongo import ReturnDocument
    conn, col = _tokens()
    now = _now()
    try:
        doc = col.find_one_and_update(
            {"_id": token, "uid": int(uid), "gate_version": 2, "issued": True,
             "used": False, "created": {"$gte": now - TOKEN_TTL, "$lte": now}},
            {"$set": {"used": True, "used_at": now}},
            return_document=ReturnDocument.AFTER)
        if not doc:
            return None
        hours = ttl_hours()
        # Atomic update pipeline keeps counts race-safe across different
        # fresh links for one user. All grants use this one implementation.
        conn.db["shortener_unlocks"].update_one({"_id": int(uid)}, [{"$set": {
            "gate_version": 2, "unlocked_until": now + hours * 3600,
            "last_unlock": now, "day": _today(),
            "visits_total": {"$add": [{"$ifNull": ["$visits_total", 0]}, 1]},
            "visits_today": {"$add": [{"$cond": [
                {"$eq": ["$day", _today()]}, {"$ifNull": ["$visits_today", 0]}, 0]}, 1]},
        }}], upsert=True)
        return int(uid)
    except Exception as exc:
        # A failed redemption may need a fresh link, but never says Verified.
        log.warning("shortener: redemption failed (%s); no success response", type(exc).__name__)
        return None
    finally:
        conn.close()


def _urlencode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


def require_access(uid: int, admin_user_id: int) -> None:
    """Enforce action access without blocking metadata browsing.

    Explicit OFF/admin bypass only. Other failures never unlock actions.
    """
    if not is_verified(uid, admin_user_id):
        from fastapi import HTTPException
        raise HTTPException(403, detail={
            "code": "shortener_required",
            "message": "Verification required — tap Read or Download to unlock.",
        })
