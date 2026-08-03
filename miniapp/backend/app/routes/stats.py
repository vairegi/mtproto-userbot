"""
stats.py — /api/admin/stats

Aggregate KPIs for the admin panel: total users, active users today,
top queuers, bookmark hotlist, ban count.

Every field here is computed on-demand from Mongo. Cheap enough for the
handful of times an admin opens the panel; if it ever gets slow, cache
the result for 60s in `miniapp_settings.stats_cache`.

Adding a new KPI:
  1. Add a helper below.
  2. Add it to the returned dict.
  3. Frontend admin.js can render the new key without a schema change.
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends

from .. import db
from ..auth import require_admin

router = APIRouter(prefix="/api/admin/stats", tags=["admin"])


def _today_str() -> str:
    return _dt.datetime.utcnow().date().isoformat()


@router.get("")
def stats(_a: dict = Depends(require_admin)) -> dict:
    users_coll = db.col_users()
    usage_coll = db.col_usage()
    bm_coll    = db.col_bookmarks()

    total_users     = users_coll.count_documents({})
    banned_users    = users_coll.count_documents({"banned": True})
    active_today    = usage_coll.count_documents({"date": _today_str()})
    total_bookmarks = bm_coll.count_documents({})

    # Top 5 queuers today
    top_today = list(usage_coll.aggregate([
        {"$match": {"date": _today_str()}},
        {"$sort": {"count": -1}},
        {"$limit": 5},
        {"$project": {"user_id": 1, "count": 1, "_id": 0}},
    ]))

    # Top 5 bookmarked galleries (across all users)
    top_bookmarks = list(bm_coll.aggregate([
        {"$group": {
            "_id": "$gallery_id",
            "count": {"$sum": 1},
            "title": {"$first": "$title"},
            "cover": {"$first": "$cover"},
        }},
        {"$sort": {"count": -1}},
        {"$limit": 5},
    ]))

    return {
        "totals": {
            "users":       total_users,
            "banned":      banned_users,
            "bookmarks":   total_bookmarks,
            "active_today": active_today,
        },
        "top_queuers_today": top_today,
        "top_bookmarks": [
            {"gallery_id": r.get("_id"), "count": r.get("count"),
             "title": r.get("title"), "cover": r.get("cover")}
            for r in top_bookmarks
        ],
        "settings": {
            "public_mode":         db.get_public_mode(),
            "default_daily_limit": db.get_default_daily(),
            "default_cooldown_s":  db.get_default_cooldown(),
        },
    }
