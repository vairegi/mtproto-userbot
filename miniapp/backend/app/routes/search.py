"""
search.py — /api/search

Thin wrapper around services/scraper_bridge.search. Every filter parameter
maps 1:1 to a query-string field the frontend's search-operators plugin
produces, so adding a new operator on either side is a single change.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from ..auth import get_current_user
from ..services import scraper_bridge

router = APIRouter(prefix="/api/search", tags=["search"])


def _csv(s: Optional[str]) -> list[str]:
    if not s: return []
    return [x.strip().lower() for x in s.split(",") if x.strip()]


@router.get("")
def search(
    q: str = "",
    include_tags: str = "",
    exclude_tags: str = "",
    artist: str = "",
    pages_min: Optional[int] = Query(None),
    pages_max: Optional[int] = Query(None),
    sort: str = "popular",
    lang: str = "english",
    page: int = 1,
    per_page: int = 25,
    _user: dict = Depends(get_current_user),
) -> dict:
    include = _csv(include_tags)
    exclude = _csv(exclude_tags)
    if artist:
        include.append(f"artist:{artist.lower()}")
    items = scraper_bridge.search(
        q=q, page=page, sort=sort, lang=lang,
        include_tags=include, exclude_tags=exclude,
        pages_min=pages_min, pages_max=pages_max,
        per_page=per_page,
    )
    return {"items": items, "page": page, "per_page": per_page}
