"""
random.py — /api/random   (v11.7 tag-aware)

Behaviour:
  * Default:                random popular English gallery (unchanged).
  * ?respect_tags=1:        pick a gallery whose tags overlap with the
                            caller's top-3 saved tags. Falls back to
                            popular if the user has no bookmarks yet.
"""
from __future__ import annotations

import random

from fastapi import APIRouter, Depends, HTTPException

from .. import db
from ..auth import get_current_user
from ..services import scraper_bridge

router = APIRouter(prefix="/api/random", tags=["random"])


@router.get("")
def random_gallery(
    respect_tags: int = 0,
    user: dict = Depends(get_current_user),
) -> dict:
    """Return one random gallery. See module docstring for tag-aware mode."""
    # Tag-aware mode ------------------------------------------------------
    if respect_tags:
        top_tags = db.top_user_tags(int(user["id"]), limit=3)
        if top_tags:
            # Query nhentai with the user's #1 tag; sample from the pool.
            tag = random.choice(top_tags)
            try:
                items = scraper_bridge.search(
                    q=tag, page=random.randint(1, 3),
                    sort="popular", lang="english", per_page=25,
                )
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"Upstream failed: {e}")
            if items:
                pick = random.choice(items)
                pick["_reason"] = f"you liked {tag}"
                return pick
            # Fall through to default if the tag returned nothing.

    # Default: popular random --------------------------------------------
    try:
        items = scraper_bridge.search(
            q="", page=random.randint(1, 5),
            sort="popular", lang="english", per_page=25,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Upstream failed: {e}")
    if not items:
        raise HTTPException(404, "No galleries available")
    return random.choice(items)
