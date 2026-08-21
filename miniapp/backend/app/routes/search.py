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
# v12.34 (Task 1): shared badge helper (used by every list route).
from ._badge import attach_is_cached

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
    # v12.1 (B/C): request meta so the frontend can render a truthful
    # Next-Page button (has_more) and — when useful — surface the fact that
    # some upstream pages were rate-limited (so the user knows retrying may
    # yield more results).
    result = scraper_bridge.search(
        q=q, page=page, sort=sort, lang=lang,
        include_tags=include, exclude_tags=exclude,
        pages_min=pages_min, pages_max=pages_max,
        per_page=per_page, _return_meta=True,
    )
    if isinstance(result, dict):
        items = result.get("items") or []
        has_more = bool(result.get("has_more"))
        rate_limited_pages = result.get("upstream_rate_limited_pages") or []
    else:  # defensive: older bridge returned a list
        items = result
        has_more = len(items) >= per_page
        rate_limited_pages = []
    # v12.34 (Task 1): attach is_cached flag per card. One Mongo query for
    # the whole page (covered by _id index) — no N+1. Failure is silent:
    # badges are cosmetic and must never break a search.
    attach_is_cached(items)
    return {
        "items": items,
        "page": page,
        "per_page": per_page,
        "has_more": has_more,
        "upstream_rate_limited": bool(rate_limited_pages),
    }
