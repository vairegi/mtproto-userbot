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
    shortener_limit          per-day completed-visit cap, default 1  (/shortenerlimit)
    shortener_hours          unlock TTL hours, default 6             (/setverifytime)
    shortener_app_msg        mini-app overlay text                   (/shortenermsg)
    shortener_bot_msg        bot DM text                             (/shortenerbotmsg)
    shortener_buttons        JSON list [{"label","url"}]             (/shortenerbtn)

Per-user state lives in two Mongo collections (best-effort writes,
epoch seconds — floats, per the shared §5.5 contract):

    shortener_tokens   {_id: token, uid, created, used}
    shortener_unlocks  {_id: uid, unlocked_until: float, visits_today: int,
                        day: "YYYY-MM-DD", visits_total: int}

The gate FAILS OPEN everywhere: any DB / API / config error means the user
is let in. The shortener is an earning layer, never a lockout.
"""
from __future__ import annotations

import json
import logging

import secrets
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
DEFAULT_LIMIT = 1            # one completed visit unlocks
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
    except Exception:  # noqa: BLE001
        return default
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def is_enabled() -> bool:
    return _flag("shortener_enabled", "0") == "1"


def api_url() -> str:
    return _flag("shortener_api_url", "").strip()


def limit_per_day() -> int:
    try:
        n = int(_flag("shortener_limit", str(DEFAULT_LIMIT)))
        return n if n > 0 else DEFAULT_LIMIT
    except (TypeError, ValueError):
        return DEFAULT_LIMIT


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
    """True → user may use the app / download. False → show the gate.

    Fail-open on any error. Admins always pass. Disabled / misconfigured
    shortener always passes.
    """
    try:
        if not is_enabled():
            return True
        if _is_admin(uid, admin_user_id):
            return True
        if not api_url():
            # No provider configured → nothing to earn with → let users in.
            return True
        conn, col = _unlocks()
        try:
            doc = col.find_one({"_id": int(uid)})
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if not doc:
            return False
        until = float(doc.get("unlocked_until", 0) or 0)
        return _now() < until
    except Exception:  # noqa: BLE001
        return True  # fail open


def status_for(uid: int, admin_user_id: int) -> Dict[str, Any]:
    """Payload for GET /api/shortener/status."""
    verified = is_verified(uid, admin_user_id)
    return {
        "enabled": is_enabled(),
        "verified": verified,
        "locked": is_enabled() and not verified,
        "message": app_message(),
        "buttons": extra_buttons(),
        "unlock_button": UNLOCK_BUTTON_LABEL,
        "hours": ttl_hours(),
    }


# ---------------------------------------------------------------------------
# Token mint / verify
# ---------------------------------------------------------------------------
def mint_token(uid: int) -> str:
    token = secrets.token_urlsafe(16)
    conn, col = _tokens()
    try:
        col.update_one(
            {"_id": token},
            {"$set": {"uid": int(uid), "created": _now(), "used": False}},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return token


def verify_token(token: str) -> Optional[int]:
    """Consume a token → unlock its owner. Returns uid or None.

    Idempotent per token: a token may be used once. Unlock TTL and the
    per-day visit cap are applied here.
    """
    if not token:
        return None
    conn, col = _tokens()
    try:
        doc = col.find_one({"_id": token})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    if not doc:
        return None
    try:
        created = float(doc.get("created", 0) or 0)
        if created and _now() - created > TOKEN_TTL:
            return None
        if doc.get("used"):
            # already consumed → still return uid so the bot can re-confirm,
            # but do NOT double-count the visit.
            return int(doc.get("uid"))
        uid = int(doc.get("uid"))
    except Exception:  # noqa: BLE001
        return None

    now = _now()
    until = now + ttl_hours() * 3600
    today = _today()

    # mark token used (best effort)
    conn, col = _tokens()
    try:
        col.update_one({"_id": token}, {"$set": {"used": True, "used_at": now}})
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    # record unlock + visit (respect per-day cap for counting, but always
    # unlock — the user did complete the link)
    conn, ucol = _unlocks()
    try:
        doc = ucol.find_one({"_id": uid}) or {}
        visits_today = int(doc.get("visits_today", 0) or 0)
        day = doc.get("day", "")
        if day != today:
            visits_today = 0
            day = today
        visits_today += 1
        ucol.update_one(
            {"_id": uid},
            {"$set": {
                "unlocked_until": until,
                "visits_today": visits_today,
                "day": day,
                "visits_total": int(doc.get("visits_total", 0) or 0) + 1,
                "last_unlock": now,
            }},
            upsert=True,
        )
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    return uid


# ---------------------------------------------------------------------------
# VPLINK short-url generation (fail-open: on error return the raw deep link)
# ---------------------------------------------------------------------------
def shorten(deep_link: str) -> str:
    """Return a shortened URL for deep_link via the configured provider.

    VPLINK format:  GET https://vplink.in/api?api=TOKEN&url=LONGURL
    Response JSON:  {"status":"success","shortenedUrl":"https://vplink.in/xxxx"}

    On ANY failure (no api url, http error, bad json) we return the raw
    deep_link so the user can still verify directly — fail open.
    """
    base = api_url()
    if not base:
        return deep_link
    try:
        # Build the request URL: ensure the long link lands as the `url=` value.
        # The admin may paste the api base with or without a trailing `url=`.
        if "url=" in base:
            head = base.split("url=")[0] + "url="
        else:
            head = base + ("" if base.endswith(("?", "&")) else ("&" if "?" in base else "?")) + "url="
        target = head + _urlencode(deep_link)
        r = httpx.get(target, timeout=10.0, follow_redirects=True)
        data = r.json()
        short = (
            data.get("shortenedUrl")
            or data.get("shortened_url")
            or data.get("short")
            or data.get("url")
            or ""
        )
        if isinstance(short, str) and short.startswith("http"):
            return short
    except Exception as e:  # noqa: BLE001
        log.warning("shortener: provider call failed (%s) — failing open", e)
    return deep_link


def _urlencode(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")
