"""
suggestions.py — /api/gallery/{id}/suggestions

Returns 6 "Similar to this" cards for a gallery, ranked by overlapping tags.

v12.34i design decisions
------------------------
* Backed by nhentai's `?include=suggestions` payload, which hf_scraper.py
  already fetches inside fetch_gallery_meta. We call the direct endpoint
  here so we get the raw `suggestions[]` array (fetch_gallery_meta drops
  it by design — it only returns the target gallery).
* Cache key `suggest:<gid>` re-uses the existing nhentai_cache TTL
  (`TTL_SUGGEST_SEC` = 3 days) and the never-expire kill-switch shipped
  in `5ad1ba8`. Warm on first read; instant on every subsequent read.
* Zero mutation. Never touches Mongo `galleries`. Never enqueues.
* Returns the SAME card dict shape /api/search returns
  (`id, title, cover, pages, tags, is_cached`) so the frontend can reuse
  the existing `<du-card>` component with no shape adapter.
* Fail-open: on ANY upstream / cache / parse error, returns `[]` so the
  detail sheet just hides the "Similar to this" row instead of erroring.
"""
from __future__ import annotations

import json
import logging
from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException, Query

from ..auth import get_current_user
from ..services import scraper_bridge
from ..routes._badge import attach_is_cached

log = logging.getLogger("miniapp.suggestions")

router = APIRouter(prefix="/api/gallery", tags=["gallery"])


@router.get("/{gallery_id}/suggestions")
def gallery_suggestions(
    gallery_id: str,
    limit: int = Query(default=6, ge=1, le=12),
    _user: dict = Depends(get_current_user),
) -> dict:
    """Return up to `limit` similar-to-this cards.

    Response shape (mirrors /api/search's cardish shape so the frontend
    grid/card component works unchanged):
        {
          "items": [ {id, title, cover, pages, tags, is_cached}, ... ],
          "gallery_id": "<gid>",
          "count": N
        }
    """
    # v12.57: kill switch — this endpoint ran a full-table json_each
    # scan (all ~15k gallery rows PER REQUEST), a top Turso read burner.
    # Disabled by default; set SIMILAR_ENABLED=1 to re-enable.
    import os as _os_sim
    if _os_sim.environ.get("SIMILAR_ENABLED", "0").strip().lower() not in ("1", "true", "on"):
        return {"ok": False, "error": "similar-disabled", "items": [], "results": []}
    try:
        items = scraper_bridge.gallery_suggestions(str(gallery_id), int(limit))
    except Exception as e:  # noqa: BLE001
        # Fail-open: log and return empty so the sheet just hides the row.
        log.warning("gallery_suggestions(%s) failed: %s", gallery_id, e)
        items = []

    if not isinstance(items, list):
        items = []

    # Cap to requested limit even if the source returned more.
    items = items[: int(limit)]

    # v12.34: attach ⚡⚡ / 📥 badge. Silent on any error (badges are cosmetic).
    try:
        attach_is_cached(items)
    except Exception:  # noqa: BLE001
        pass

    return {
        "items": items,
        "gallery_id": str(gallery_id),
        "count": len(items),
    }
