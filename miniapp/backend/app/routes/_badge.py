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

import sys
from typing import Any, List

# v12.35 (Task 2): the previous `from .. import db` bound _midb to
# miniapp/backend/app/db.py — a Local stub scoped to miniapp_* collections
# only, with NO `connect()`, NO `galleries` collection, NO
# `get_cached_gallery_ids()`. Every call into attach_is_cached() then
# silently raised AttributeError inside the broad `except Exception`, so
# NO card on ANY list page ever received an `is_cached` field — that's
# why the badges never showed up in the mini-app even though the frontend
# fallback was already `undefined`.
#
# The repo-root db.py (APP_DIR/db.py) IS on PYTHONPATH via start.sh line
# 22 + 170 and OWNS `connect()` / `get_cached_gallery_ids()` / the
# `galleries` collection that relay_v2 writes to. Use the top-level
# import and prepend the repo root first so we always pick that one,
# regardless of where the request happens to be mounted.
_HERE = __file__
# __file__ = .../miniapp/backend/app/routes/_badge.py  →  repo root is 4 levels up
_REPO_ROOT = __import__("os").path.abspath(
    __import__("os").path.join(_HERE, "..", "..", "..", "..", "..")
)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
import db as _midb  # noqa: E402,F401


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
