"""
trending.py — /api/trending  (v11.7)

Two GET endpoints for the home tab:

  GET /api/trending/tags       — top tags across all users' recent bookmarks
                                 query: ?days=7&limit=12
  GET /api/trending/galleries  — nhentai popular-week feed, cached upstream
                                 query: ?page=1&per_page=25
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..auth import get_current_user
from ..services import scraper_bridge

router = APIRouter(prefix="/api/trending", tags=["trending"])


@router.get("/tags")
def trending_tags_endpoint(
    days:  int = 7,
    limit: int = 12,
    _user: dict = Depends(get_current_user),
) -> dict:
    rows = db.trending_tags(limit=limit, days=days)
    return {"items": rows, "days": int(max(1, days))}


@router.get("/galleries")
def trending_galleries_endpoint(
    page:     int = 1,
    per_page: int = 25,
    _user: dict = Depends(get_current_user),
) -> dict:
    try:
        items = scraper_bridge.search(
            q="", page=int(max(1, page)),
            sort="popular-week", lang="english", per_page=int(max(1, min(50, per_page))),
        )
    except Exception:
        items = []
    # v12.34 (Task 1): ⚡⚡ / 📥 badge flag on each card.
    from ._badge import attach_is_cached
    attach_is_cached(items)
    return {"items": items or []}
