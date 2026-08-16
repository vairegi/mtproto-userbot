"""
auth.py — Telegram initData HMAC verification.

Every request from the Mini App carries an `X-Telegram-Init-Data` header
containing the raw initData string Telegram provided to the WebApp. This
module verifies that string against BOT_TOKEN and returns a parsed user dict.

Reference: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
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


def _compute_hash(init_data: str, bot_token: str) -> tuple[str, dict]:
    """Return (expected_hex_hash, parsed_fields_without_hash)."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    data_check_string = "\n".join(
        f"{k}={pairs[k]}" for k in sorted(pairs.keys())
    )
    secret_key = hmac.new(
        b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256
    ).digest()
    computed = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return computed, {**pairs, "hash": received_hash}


def verify_init_data(init_data: str) -> Optional[dict]:
    """Return the parsed user dict if the signature is valid, else None."""
    if not init_data or not settings.bot_token:
        return None
    try:
        expected, fields = _compute_hash(init_data, settings.bot_token)
    except Exception:
        return None
    received = fields.get("hash", "")
    if not hmac.compare_digest(expected, received):
        return None

    # Reject signatures older than 24h to limit replay windows.
    auth_date = int(fields.get("auth_date", "0") or "0")
    if auth_date and (time.time() - auth_date) > 86400:
        return None

    user_raw = fields.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except Exception:
        return None


async def get_current_user(
    x_telegram_init_data: str = Header(default=""),
) -> dict:
    """FastAPI dependency: raises 401 if initData is missing/invalid."""
    user = verify_init_data(x_telegram_init_data)
    if not user:
        # DEV escape hatch: if BOT_TOKEN is empty (running locally without a
        # real Telegram context), synthesize the admin user. NEVER enable
        # this in prod — leaving BOT_TOKEN empty in prod would be the same
        # as leaving the door wide open.
        if not settings.bot_token and settings.admin_user_id:
            return {"id": settings.admin_user_id, "first_name": "DevAdmin",
                    "username": "dev", "is_dev": True}
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad initData")
    return user


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """FastAPI dependency: 403 for non-admins."""
    if int(user.get("id", 0)) != int(settings.admin_user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user


async def require_admin_key(x_admin_key: str = Header(default="")) -> dict:
    """v12.25: static-key-only auth for maintenance endpoints.

    Accepts the ADMIN_STATIC_KEY env value via the `X-Admin-Key` header
    (or the `admin_key` query param). Uses a constant-time compare so the
    key can't be recovered via timing. Returns a synthetic admin dict so
    callers can use it wherever require_admin is expected — but NOTE: it
    does NOT do Telegram initData verification, so it must only gate
    read-only / cache-rewrite routes, never user-data or state-mutating
    routes.

    Disabled when ADMIN_STATIC_KEY is unset (empty) — always 401 then.
    """
    key = settings.admin_static_key or ""
    if not key:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Static admin key is not configured",
        )
    incoming = (x_admin_key or "").strip()
    if not incoming:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing X-Admin-Key")
    if not hmac.compare_digest(incoming, key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad X-Admin-Key")
    return {
        "id": int(settings.admin_user_id),
        "first_name": "StaticKeyAdmin",
        "username": "static_key",
        "is_static_key": True,
    }


async def require_admin_or_key(
    x_admin_key: str = Header(default=""),
    x_telegram_init_data: str = Header(default=""),
) -> dict:
    """v12.25: accept EITHER a valid static key OR the normal Telegram
    initData admin session. Used by the cache maintenance endpoints so they
    work both from inside the Mini App (initData) and from curl/scripts
    (X-Admin-Key), whichever is configured.

    Decision order:
      1. If a static key was supplied, validate it; if bad -> 401.
      2. Otherwise verify Telegram initData directly and check the admin
         user id (must NOT go through get_current_user, which would raise
         401 for a curl request that has no initData header yet).
    """
    incoming = (x_admin_key or "").strip()
    if incoming:
        return await require_admin_key(x_admin_key=incoming)
    user = verify_init_data(x_telegram_init_data)
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Bad initData")
    if int(user.get("id", 0)) != int(settings.admin_user_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin only")
    return user
