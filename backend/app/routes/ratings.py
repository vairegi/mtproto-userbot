"""
ratings.py — /api/ratings  (v11.7)

Star ratings (1..5) for galleries.

  GET    /api/ratings/{gallery_id}         → {avg, count, dist, my_stars}
  POST   /api/ratings/{gallery_id}         body: {stars: 1..5}
  DELETE /api/ratings/{gallery_id}         clear the caller's vote
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/ratings", tags=["ratings"])


class RatingBody(BaseModel):
    stars: int


@router.get("/{gallery_id}")
def get_rating(gallery_id: str, user: dict = Depends(get_current_user)) -> dict:
    agg = db.get_aggregate_rating(gallery_id)
    my  = db.get_user_rating(int(user["id"]), gallery_id)
    return {**agg, "my_stars": my}


@router.post("/{gallery_id}")
def set_rating(gallery_id: str, body: RatingBody,
               user: dict = Depends(get_current_user)) -> dict:
    if not (1 <= int(body.stars) <= 5):
        raise HTTPException(400, "stars must be 1..5")
    db.set_rating(int(user["id"]), gallery_id, int(body.stars))
    return {"ok": True, **db.get_aggregate_rating(gallery_id),
            "my_stars": int(body.stars)}


@router.delete("/{gallery_id}")
def clear_rating(gallery_id: str, user: dict = Depends(get_current_user)) -> dict:
    db.clear_rating(int(user["id"]), gallery_id)
    return {"ok": True, **db.get_aggregate_rating(gallery_id),
            "my_stars": None}
