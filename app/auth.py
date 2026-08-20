"""
auth.py — admin-key gate for HTTP admin routes + Telegram user-id gate
for chat commands.

HTTP: pass `?key=<BOT1_ADMIN_KEY>` or header `X-Admin-Key: <BOT1_ADMIN_KEY>`.
Telegram: only user IDs in BOT1_ADMIN_USER_IDS can run /pause /resume /trigger.
"""
from __future__ import annotations

from fastapi import Header, HTTPException, Query

from .config import settings


def require_admin(
    key: str | None = Query(default=None),
    x_admin_key: str | None = Header(default=None, alias="X-Admin-Key"),
) -> None:
    if not settings.admin_key:
        # Refuse to run admin ops without a configured key — safer default.
        raise HTTPException(status_code=503, detail="BOT1_ADMIN_KEY not configured")
    got = (x_admin_key or key or "").strip()
    if got != settings.admin_key:
        raise HTTPException(status_code=401, detail="bad admin key")


def is_tg_admin(user_id: int | None) -> bool:
    if not user_id:
        return False
    return int(user_id) in settings.admin_user_ids
