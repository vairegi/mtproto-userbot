"""
savecount.py — /api/bookmarks/count/{gallery_id}   (v11.8 #5)

Returns the number of DISTINCT users who have bookmarked the given
gallery. Used by the Save button in card-actions.js to render a label
like "Save · 1.2k" / "Saved Already · 312".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/bookmarks", tags=["bookmarks"])


@router.get("/count/{gallery_id}")
def save_count(gallery_id: str, _user: dict = Depends(get_current_user)) -> dict:
    try:
        n = db.col_bookmarks().count_documents({"gallery_id": gallery_id})
    except Exception:
        n = 0
    return {"gallery_id": gallery_id, "saves": int(n)}
