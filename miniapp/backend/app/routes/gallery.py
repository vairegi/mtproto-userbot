"""
gallery.py — /api/gallery/{id}

Returns full detail for a single gallery. Frontend uses this when the user
taps a card and opens the detail sheet (so we can show more tags than the
search grid carries).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user
from ..services import scraper_bridge

router = APIRouter(prefix="/api/gallery", tags=["gallery"])


@router.get("/{gallery_id}")
def get_gallery(gallery_id: str, _user: dict = Depends(get_current_user)) -> dict:
    detail = scraper_bridge.gallery_detail(gallery_id)
    if not detail or not detail.get("id"):
        raise HTTPException(404, "Gallery not found")
    return detail
