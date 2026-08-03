"""
admin.py — /api/admin/* endpoints (admin-only)

Every route here uses require_admin, which returns 403 for non-admin callers.
Frontend's admin.js panel is powered by these endpoints.

Endpoints:
  GET  /api/admin/visibility                        → { public_mode }
  POST /api/admin/visibility  { public_mode }       → set visibility
  GET  /api/admin/ratelimit/defaults                → { daily, cooldown_s }
  POST /api/admin/ratelimit/defaults  { daily, cooldown_s }
  GET  /api/admin/users                             → list users + usage
  POST /api/admin/users/{uid}/reset                 → reset today's usage
  POST /api/admin/users/{uid}/limit  { daily }      → override daily limit
  POST /api/admin/users/{uid}/ban                   → ban user
  POST /api/admin/users/{uid}/unban                 → unban user
  GET  /api/admin/diag                              → scraper + queue probe
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import db
from ..auth import require_admin
from ..services import scraper_bridge, queue_bridge

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---- Visibility ----
class VisibilityBody(BaseModel):
    public_mode: bool


@router.get("/visibility")
def get_visibility(_a: dict = Depends(require_admin)) -> dict:
    return {"public_mode": db.get_public_mode()}


@router.post("/visibility")
def set_visibility(body: VisibilityBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_setting("public_mode", bool(body.public_mode))
    return {"ok": True, "public_mode": bool(body.public_mode)}


# ---- Rate limit defaults ----
class RLDefaultsBody(BaseModel):
    daily: int = 20
    cooldown_s: int = 0


@router.get("/ratelimit/defaults")
def get_rl_defaults(_a: dict = Depends(require_admin)) -> dict:
    return {
        "daily":      db.get_default_daily(),
        "cooldown_s": db.get_default_cooldown(),
    }


@router.post("/ratelimit/defaults")
def set_rl_defaults(body: RLDefaultsBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_setting("default_daily_limit", max(0, int(body.daily)))
    db.set_setting("default_cooldown_s",  max(0, int(body.cooldown_s)))
    return {"ok": True, **body.model_dump()}


# ---- Users ----
@router.get("/users")
def list_users(_a: dict = Depends(require_admin)) -> dict:
    rows = db.list_users(limit=200)
    items = []
    for r in rows:
        uid = int(r.get("_id"))
        items.append({
            "user_id":    uid,
            "first_name": r.get("first_name"),
            "username":   r.get("username"),
            "photo_url":  r.get("photo_url"),
            "banned":     bool(r.get("banned", False)),
            "limit":      db.get_user_daily_limit(uid),
            "used_today": db.get_used_today(uid),
            "last_seen":  r.get("last_seen").isoformat() if r.get("last_seen") else None,
        })
    return {"items": items}


class UserLimitBody(BaseModel):
    daily: int


@router.post("/users/{uid}/reset")
def reset_user(uid: int, _a: dict = Depends(require_admin)) -> dict:
    db.reset_used_today(uid)
    return {"ok": True}


@router.post("/users/{uid}/limit")
def set_user_limit(uid: int, body: UserLimitBody, _a: dict = Depends(require_admin)) -> dict:
    db.set_user_daily_limit(uid, max(0, int(body.daily)))
    return {"ok": True, "daily": body.daily}


@router.post("/users/{uid}/ban")
def ban(uid: int, _a: dict = Depends(require_admin)) -> dict:
    db.set_banned(uid, True)
    return {"ok": True}


@router.post("/users/{uid}/unban")
def unban(uid: int, _a: dict = Depends(require_admin)) -> dict:
    db.set_banned(uid, False)
    return {"ok": True}


# ---- Diagnostics ----
@router.get("/diag")
def diag(_a: dict = Depends(require_admin)) -> dict:
    return {
        "scraper": scraper_bridge.route_status(),
        "queue":   queue_bridge.status_summary(),
        "settings": {
            "public_mode":  db.get_public_mode(),
            "default_daily": db.get_default_daily(),
            "default_cooldown_s": db.get_default_cooldown(),
        },
    }
