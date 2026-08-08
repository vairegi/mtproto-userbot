"""
improvements.py — /api/improvements   (v11.8 #8)

Admin-authored changelog / "What's new" messages surfaced inside the
mini-app Settings tab.

    GET  /api/improvements?limit=50   — any signed-in user
    POST /api/improvements            — admin-only, body: {text: str}
    DELETE /api/improvements/{id}     — admin-only

Backed by the `miniapp_improvements` Mongo collection:
    { _id, text, author_id, author_name, ts }
"""
from __future__ import annotations

import datetime as _dt
import uuid as _uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..auth import get_current_user, require_admin

router = APIRouter(prefix="/api/improvements", tags=["improvements"])


def _col():
    return db.db()["miniapp_improvements"]


class ImpBody(BaseModel):
    text: str


@router.get("")
def list_improvements(
    limit: int = 50,
    _user: dict = Depends(get_current_user),
) -> dict:
    limit = int(max(1, min(200, limit)))
    try:
        rows = list(_col().find({}, {"_id": 1, "text": 1, "ts": 1,
                                     "author_name": 1})
                          .sort("ts", -1)
                          .limit(limit))
    except Exception:
        rows = []
    items = [{
        "id":     r.get("_id"),
        "text":   r.get("text") or "",
        "ts":     (r.get("ts").isoformat() if r.get("ts") else ""),
        "author": r.get("author_name") or "",
    } for r in rows]
    return {"items": items}


@router.post("")
def add_improvement(body: ImpBody, admin: dict = Depends(require_admin)) -> dict:
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 2000:
        raise HTTPException(400, "text must be \u2264 2000 chars")
    doc = {
        "_id":         str(_uuid.uuid4()),
        "text":        text,
        "author_id":   int(admin.get("id") or 0),
        "author_name": admin.get("first_name") or admin.get("username") or "admin",
        "ts":          _dt.datetime.utcnow(),
    }
    _col().insert_one(doc)
    return {"ok": True, "id": doc["_id"]}


@router.delete("/{imp_id}")
def delete_improvement(imp_id: str, _admin: dict = Depends(require_admin)) -> dict:
    r = _col().delete_one({"_id": imp_id})
    return {"ok": True, "deleted": r.deleted_count}
