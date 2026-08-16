"""
bookmarks.py — /api/bookmarks CRUD

GET  /api/bookmarks              list caller's bookmarks
POST /api/bookmarks               add one   (body: {id, title, cover, pages, tags})
DELETE /api/bookmarks/{gid}       remove one
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Any, Optional

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/bookmarks", tags=["bookmarks"])


class BookmarkBody(BaseModel):
    id: Any
    title: Optional[str] = None
    cover: Optional[str] = None
    pages: Optional[int] = None
    tags:  Optional[list] = None


@router.get("")
def list_bm(user: dict = Depends(get_current_user)) -> dict:
    uid = int(user["id"])
    rows = db.list_bookmarks(uid)
    items = [{
        "id":    r.get("gallery_id"),
        "title": r.get("title"),
        "cover": r.get("cover"),
        "pages": r.get("pages"),
        "tags":  r.get("tags") or [],
    } for r in rows]
    return {"items": items}


@router.post("")
def add_bm(body: BookmarkBody, user: dict = Depends(get_current_user)) -> dict:
    uid = int(user["id"])
    db.add_bookmark(uid, body.model_dump())
    return {"ok": True}


@router.delete("/{gallery_id}")
def del_bm(gallery_id: str, user: dict = Depends(get_current_user)) -> dict:
    uid = int(user["id"])
    db.remove_bookmark(uid, gallery_id)
    return {"ok": True}
