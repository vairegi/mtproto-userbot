"""
auth.py — Telegram initData HMAC verification.

Every request from the Mini App carries an `X-Telegram-Init-Data` header
containing the raw initData string Telegram provided to the WebApp. This
module verifies that string against BOT_TOKEN and returns a parsed user dict.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

v0.3 security changes:
  1. Missing BOT_TOKEN in production -> 503, NOT an admin session.
  2. Dev bypass requires settings.dev_bypass_enabled (MINIAPP_DEV_MODE=1 AND
     empty BOT_TOKEN AND a configured admin).
  3. Replay window 24h -> settings.auth_max_age_s (default 1h).
  4. A missing / non-numeric / future auth_date is now REJECTED. Previously
     `if auth_date and ...` meant "no auth_date" sailed straight through.
  5. Multi-admin via settings.is_admin().
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qsl

from fastapi import Depends, Header, HTTPException, status

from .config import settings

# Small tolerance for client/server clock skew (seconds).
_CLOCK_SKEW_S = 300


def _compute_hash(init_data: str, bot_token: str) -> tuple[str, dict]:
    """Return (expected_hex_hash, parsed_fields_including_hash)."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    data_check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    computed = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return computed, {**pairs, "hash": received_hash}


def verify_init_data(init_data: str) -> Optional[dict]:
    """Return the parsed user dict if the signature is valid AND fresh."""
    if not init_data or not settings.bot_token:
        return None
    try:
        expected, fields = _compute_hash(init_data, settings.bot_token)
    except Exception:
        return None

    received = fields.get("hash", "")
    if not received or not hmac.compare_digest(expected, received):
        return None

    # --- Freshness. A missing or malformed auth_date is a hard reject. ---
    raw_date = fields.get("auth_date", "")
    try:
        auth_date = int(raw_date)
    except (TypeError, ValueError):
        return None
    if auth_date <= 0:
        return None

    now = time.time()
    age = now - auth_date
    if age > settings.auth_max_age_s:
        return None
    if age < -_CLOCK_SKEW_S:      # timestamp from the future
        return None

    user_raw = fields.get("user")
    if not user_raw:
        return None
    try:
        user = json.loads(user_raw)
    except Exception:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    return user


async def get_current_user(
    x_telegram_init_data: str = Header(default=""),
) -> dict:
    """FastAPI dependency: raises 401 if initData is missing/invalid."""
    if not settings.bot_token:
        # Local dev with no Telegram context — explicit opt-in only.
        if settings.dev_bypass_enabled:
            return {
                "id": settings.admin_user_id,
                "first_name": "DevAdmin",
                "username": "dev",
                "is_dev": True,
            }
        # Prod misconfiguration: refuse to serve rather than trust anyone.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Auth unavailable: BOT_TOKEN is not configured.",
        )

    user = verify_init_data(x_telegram_init_data)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad initData")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: 403 for non-admins."""
    if not settings.is_admin(user.get("id", 0)):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user
