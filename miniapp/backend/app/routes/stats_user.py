"""
stats_user.py — /api/stats/me  (v11.7)

Per-user counters + earned badges for the profile page.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/stats", tags=["stats_user"])


@router.get("/me")
def my_stats(user: dict = Depends(get_current_user)) -> dict:
    return db.user_stats(int(user["id"]))


class ShareBody(BaseModel):
    gallery_id: str


@router.post("/share")
def record_share(body: ShareBody, user: dict = Depends(get_current_user)) -> dict:
    """Called by the Share button so the 'Sharer' badge can unlock."""
    db.record_share(int(user["id"]), body.gallery_id)
    return {"ok": True}
