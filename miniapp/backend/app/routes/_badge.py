"""
_badge.py — shared helper for the v12.34 (Task 1) "is_cached" card badge.

Every list route (search, random, recommendations, bookmarks, trending)
calls attach_is_cached(items) to add a per-item `is_cached` boolean so the
Mini App can render ⚡⚡ (in DB channel, instant DM) vs 📥 (must queue).

Design notes
------------
* One Mongo find() per LIST request, covered by `galleries._id`. No N+1.
* Silent-fail: any exception leaves items untouched — badges are cosmetic
  and must NEVER 500 a list route.
* Underscore-prefixed filename so it isn't auto-included by any route
  discovery that walks routes/*.py (see routes/__init__.py registration).
"""
from __future__ import annotations

from typing import Any, List

from .. import db as _midb


def attach_is_cached(items: List[Any]) -> None:
    """Mutate `items` in place to add is_cached=True/False per row.

    `items` is expected to be a list of dicts with an "id" field (the
    nhentai gallery id). Non-dict entries and entries without an id are
    left alone. Failures never propagate.
    """
    if not items:
        return
    try:
        ids = [
            it.get("id")
            for it in items
            if isinstance(it, dict) and it.get("id") is not None
        ]
        if not ids:
            return
        conn = _midb.connect()
        try:
            cached = _midb.get_cached_gallery_ids(conn, ids)
        finally:
            conn.close()
        for it in items:
            if isinstance(it, dict) and it.get("id") is not None:
                it["is_cached"] = str(it["id"]) in cached
    except Exception:  # noqa: BLE001
        # Cosmetic; never fail the request.
        return
