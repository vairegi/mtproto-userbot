"""
profile.py — /api/profile/me

Returns the caller's identity + permission summary + rate-limit stats.
The frontend calls this on boot to know whether to show the Admin tab and
whether the app is currently in public/private mode.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db, ratelimit
from ..auth import get_current_user
from ..config import settings

router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("/me")
def me(user: dict = Depends(get_current_user)) -> dict:
    uid = int(user["id"])
    stored = db.upsert_user(user)
    public_mode = db.get_public_mode()
    is_admin = settings.is_admin(uid)  # v0.3: multi-admin aware
    rl = ratelimit.usage_summary(uid)

    # Non-admin + private mode = restricted view.
    can_queue = is_admin or public_mode

    stats = {
        "bookmarks": db.col_bookmarks().count_documents({"user_id": uid}),
        "queued":    rl["used"],
    }

    return {
        "user_id":     uid,
        "first_name":  user.get("first_name"),
        "last_name":   user.get("last_name"),
        "username":    user.get("username"),
        "photo_url":   user.get("photo_url"),
        "is_admin":    is_admin,
        "public_mode": public_mode,
        "can_queue":   can_queue,
        "rate_limit":  rl,
        "stats":       stats,
        "banned":      bool(stored.get("banned", False)),
    }
