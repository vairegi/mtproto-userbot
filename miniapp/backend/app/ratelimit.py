"""
ratelimit.py — Per-user daily quota with admin overrides.

Design:
  * Every "queue" call passes through `check_and_consume(user_id)`.
  * If the caller is the admin, the quota is bypassed.
  * Otherwise we look up their per-user override (or fall back to the global
    default), compare against today's usage counter, and increment.
  * Admin can call reset / set_limit / ban via /api/admin/users/*.

This is the ONLY module that decides whether a user is allowed to queue.
Adding a new rate rule (e.g. per-hour limit)? Add a helper here, call it
from routes/queue.py.  Nothing else changes.
"""
from __future__ import annotations

import datetime as _dt
import time
from typing import Optional

from fastapi import HTTPException, status

from . import db
from .config import settings


# In-memory cooldown table: user_id -> last_ts (float). Cleared on restart —
# that's fine; cooldown is a short-window rate smoother, not a hard quota.
_last_hit: dict[int, float] = {}


def _is_admin(user_id: int) -> bool:
    return int(user_id) == int(settings.admin_user_id)


def check_and_consume(user_id: int) -> dict:
    """
    Raise HTTPException(429) if the user has exceeded quota / is in cooldown /
    is banned. Otherwise increment usage and return {'used': N, 'limit': L}.
    """
    uid = int(user_id)

    if db.is_banned(uid) and not _is_admin(uid):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "You are banned from this app.")

    # Admin bypasses everything (still increments usage for stats).
    if _is_admin(uid):
        used = db.increment_used_today(uid)
        return {"used": used, "limit": 0, "unlimited": True}

    # Cooldown gate.
    cooldown_s = db.get_default_cooldown()
    if cooldown_s > 0:
        last = _last_hit.get(uid, 0.0)
        wait = cooldown_s - (time.time() - last)
        if wait > 0:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Cooldown: wait {int(wait)}s before queueing again.",
            )

    # Daily quota gate.  0 = unlimited (matches admin_bot.py's cooldown=0 spec).
    limit = db.get_user_daily_limit(uid)
    used = db.get_used_today(uid)
    if limit > 0 and used >= limit:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Daily limit reached ({used}/{limit}). Try again tomorrow.",
        )

    new_used = db.increment_used_today(uid)
    _last_hit[uid] = time.time()
    return {"used": new_used, "limit": limit, "unlimited": limit == 0}


def usage_summary(user_id: int) -> dict:
    """Read-only view for /api/profile/me."""
    uid = int(user_id)
    if _is_admin(uid):
        return {
            "used": db.get_used_today(uid),
            "limit": 0,
            "unlimited": True,
            "cooldown_s": 0,
            "banned": False,
        }
    return {
        "used": db.get_used_today(uid),
        "limit": db.get_user_daily_limit(uid),
        "unlimited": db.get_user_daily_limit(uid) == 0,
        "cooldown_s": db.get_default_cooldown(),
        "banned": db.is_banned(uid),
    }
