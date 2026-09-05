"""
routes/online.py — v12.64: live "users online" count for the header badge.

Counts miniapp_users whose last_seen (maintained by upsert_user on every
authenticated request) is within the last ONLINE_WINDOW_MIN minutes.
Cheap single count_documents; polled by the frontend every 30s.
"""
from __future__ import annotations

import datetime as _dt
import os

from fastapi import APIRouter, Depends

from ..auth import get_current_user
from ..db import col_users

router = APIRouter(prefix="/api", tags=["online"])

_WINDOW_MIN = int(os.getenv("ONLINE_WINDOW_MIN", "5") or 5)


@router.get("/online")
def online(_user: dict = Depends(get_current_user)) -> dict:
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(minutes=_WINDOW_MIN)
    try:
        n = col_users().count_documents({"last_seen": {"$gte": cutoff}})
    except Exception:  # noqa: BLE001
        n = 0
    return {"online": int(n), "window_min": _WINDOW_MIN}
