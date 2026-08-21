"""
stats_leaderboard.py — /api/stats/leaderboard  (v12.36)

Public (logged-in) per-user leaderboard.
Previously on the Admin tab as "Top Queuers Today" (limit 5). Moved
to the Profile tab and the cap raised to 11 users per the operator's
brief. Same data source as the admin KPI (collection join keeps it
cheap: one aggregate + one $lookup).
"""
from __future__ import annotations

import datetime as _dt

from fastapi import APIRouter, Depends

from .. import db
from ..auth import get_current_user

router = APIRouter(prefix="/api/stats", tags=["stats_leaderboard"])


# v12.36: bumped 5 -> 11 to match the new Profile-rendered list.
LEADERBOARD_CAP = 11


def _today_str() -> str:
    return _dt.datetime.utcnow().date().isoformat()


@router.get("/leaderboard")
def leaderboard(
    limit: int = LEADERBOARD_CAP,
    _user: dict = Depends(get_current_user),
) -> dict:
    """Return the top-N queue counts for today.

    Auth: any logged-in user (same gate as the rest of /api/stats/*).
    Designate via `?limit=N` (clamped 1..50) but the frontend calls without
    it so the deployment default applies.
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = LEADERBOARD_CAP
    n = max(1, min(50, n))

    usage_coll = db.col_usage()
    rows = list(usage_coll.aggregate([
        {"$match": {"date": _today_str()}},
        {"$sort": {"count": -1}},
        {"$limit": n},
        {"$lookup": {
            "from": "miniapp_users",
            "localField": "user_id",
            "foreignField": "_id",
            "as": "u",
        }},
        {"$project": {
            "user_id": 1, "count": 1, "_id": 0,
            "username":   {"$ifNull": [{"$arrayElemAt": ["$u.username", 0]}, None]},
            "first_name": {"$ifNull": [{"$arrayElemAt": ["$u.first_name", 0]}, None]},
        }},
    ]))

    return {
        "items": [
            {
                "user_id":    r.get("user_id"),
                "count":      int(r.get("count") or 0),
                "username":   r.get("username"),
                "first_name": r.get("first_name"),
            }
            for r in rows
        ],
        "date": _today_str(),
        "limit": n,
    }
