"""
random.py — /api/random

Example endpoint that demonstrates how easy it is to add a new backend
route. Just drop a file in this folder with a `router = APIRouter(...)`
and it auto-mounts on next boot.

Behavior: returns one random popular English gallery. Used by the
optional "Surprise Me" tab described in docs/PLUGIN_GUIDE.md §5.
"""
from __future__ import annotations

import random

from fastapi import APIRouter, Depends, HTTPException

from ..auth import get_current_user
from ..services import scraper_bridge

router = APIRouter(prefix="/api/random", tags=["random"])


@router.get("")
def random_gallery(_user: dict = Depends(get_current_user)) -> dict:
    """Return one random gallery from the current popular page."""
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
