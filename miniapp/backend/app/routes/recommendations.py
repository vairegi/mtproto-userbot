"""
recommendations.py — /api/recommendations  (v11.7)

"Because you saved X" — collaborative-lite recommendations built from the
current user's top-saved tags cross-referenced against other users' recent
bookmarks. See db.recommend_from_bookmarks for the algorithm.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


@router.get("")
def recommend(
    limit: int = 12,
    _user: dict = Depends(get_current_user),
) -> dict:
    uid = int(_user["id"])
    items = db.recommend_from_bookmarks(uid, limit=limit)
    top_tags = db.top_user_tags(uid, limit=3)
    return {
        "items":       items,
        "seed_tags":   top_tags,     # explains "Because you like: X, Y, Z"
        "has_seed":    bool(top_tags),
    }
