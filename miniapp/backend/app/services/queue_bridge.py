"""
queue_bridge.py — Adapter around the bot's queue_service + db.

Reuses the same MongoDB job queue the bot polls, so anything queued through
the Mini App is picked up by worker.py exactly like a /fetch from Telegram.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

log = logging.getLogger("miniapp.queue")

_HERE = os.path.dirname(os.path.abspath(__file__))
for p in [os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
          os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..")),
          "/opt/render/project/src"]:
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

try:
    import queue_service as _qs
    import db as _bot_db
    HAVE_BOT = True
except Exception as e:  # noqa: BLE001
    _qs = None
    _bot_db = None
    HAVE_BOT = False
    log.warning("queue_service / db not importable — queue endpoints will 503 (%s)", e)


def enqueue(url: str, user_id: int, username: str | None) -> dict:
    if not HAVE_BOT:
        raise RuntimeError("queue_service not available in this deployment")
    result = _qs.enqueue_batch(
        url,
        max_links=1,
        via_search=False,
        submitted_by=int(user_id),
        username=(username or "miniapp"),
        chat_id=None,
    )
    if not result or not getattr(result, "queued", None):
        raise RuntimeError("enqueue_batch returned nothing")
    job_id, gallery_url = result.queued[0][0], result.queued[0][1]
    return {"job_id": job_id, "url": gallery_url}


def status_summary() -> dict:
    if not HAVE_BOT:
        return {"pending": 0, "processing": 0, "completed": 0, "failed": 0,
                "recent": [], "error": "queue_service not loaded"}
    conn = _bot_db.connect()
    try:
        counts = _bot_db.counts_by_status(conn) or {}
        recent = _bot_db.list_recent_jobs(conn, limit=15) if hasattr(_bot_db, "list_recent_jobs") else []
        return {
            "pending":    int(counts.get("pending", 0)),
            "processing": int(counts.get("processing", 0)),
            "completed":  int(counts.get("completed", 0)),
            "failed":     int(counts.get("failed", 0)),
            "recent": [_row(r) for r in recent],
        }
    finally:
        try: conn.close()
        except Exception: pass


def _row(r: Any) -> dict:
    if isinstance(r, dict):
        return {
            "id":     r.get("id") or r.get("_id"),
            "url":    r.get("url"),
            "title":  r.get("title") or r.get("cleaned_title"),
            "status": r.get("status"),
            "user":   r.get("username") or r.get("submitted_by"),
        }
    return {"raw": str(r)}
