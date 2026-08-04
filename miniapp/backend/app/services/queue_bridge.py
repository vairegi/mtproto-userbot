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

# V2 dedup gate (docs/ARCHITECTURE_V2.md). Optional: if the parent project
# predates V2, gallery_state won't import and we silently fall back to the
# plain enqueue path so the Mini App keeps working.
try:
    import gallery_state as _gs
    HAVE_GS = True
except Exception as e:  # noqa: BLE001
    _gs = None
    HAVE_GS = False
    log.warning("gallery_state not importable — dedup gate disabled (%s)", e)


def gallery_status(url_or_id: str) -> dict:
    """Look up a gallery's V2 state WITHOUT mutating anything.

    Used by GET /api/gallery/{id}/status so the frontend can render
    "Open Post" instead of "Queue" for galleries we already have.

    Returns {"known": False} when we've never seen it, otherwise the
    status plus the deep-link when COMPLETED.
    """
    if not (HAVE_BOT and HAVE_GS):
        return {"known": False, "reason": "dedup gate unavailable"}
    gid = _gs.extract_gallery_id(url_or_id)
    if not gid:
        return {"known": False, "reason": "no gallery_id"}
    conn = _bot_db.connect()
    try:
        doc = _gs.get(conn, gid) or {}
        if not doc:
            return {"known": False, "gallery_id": gid}
        return {
            "known": True,
            "gallery_id": gid,
            "status": doc.get("status"),
            "open_link": doc.get("open_link"),
            "title": doc.get("title"),
            "pages": doc.get("pages"),
            "completed_at": doc.get("completed_at"),
            "failed_reason": doc.get("failed_reason") or "",
        }
    finally:
        try: conn.close()
        except Exception: pass


def dedup_peek(url: str) -> dict:
    """Read-only dedup pre-check used by POST /api/queue.

    Returns one of:
      {"verdict": "proceed"}                      -> caller should enqueue
      {"verdict": "already_completed", ...}       -> caller returns the link
      {"verdict": "already_processing", ...}      -> caller says "in progress"

    IMPORTANT: this does NOT claim a PROCESSING slot. Claiming happens in
    relay_v2.process_job, which is the single writer. Doing a read-only peek
    here means a Mini App tap that hits a duplicate never burns a rate-limit
    token and never creates a junk queue row.
    """
    if not (HAVE_BOT and HAVE_GS):
        return {"verdict": "proceed"}
    info = gallery_status(url)
    if not info.get("known"):
        return {"verdict": "proceed", "gallery_id": info.get("gallery_id")}

    status = (info.get("status") or "").upper()
    if status in ("COMPLETED", "PARTIAL"):
        return {
            "verdict": "already_completed",
            "gallery_id": info.get("gallery_id"),
            "status": status,
            "open_link": info.get("open_link"),
            "title": info.get("title"),
        }
    if status == "PROCESSING":
        return {
            "verdict": "already_processing",
            "gallery_id": info.get("gallery_id"),
            "status": status,
            "title": info.get("title"),
        }
    # FAILED_* tombstone → a retry is legitimate.
    return {
        "verdict": "proceed",
        "gallery_id": info.get("gallery_id"),
        "previous_status": status,
        "previous_reason": info.get("failed_reason") or "",
    }


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
    if not isinstance(r, dict):
        return {"raw": str(r)}

    out = {
        "id":     r.get("id") or r.get("_id"),
        "url":    r.get("url"),
        "title":  r.get("title") or r.get("cleaned_title"),
        "status": r.get("status"),
        "user":   r.get("username") or r.get("submitted_by"),
        # relay_v2 writes cover_link on the queue row for both fresh
        # completions AND dedup-hits, so the queue tab can render
        # "Open Post" without an extra RTT to /api/gallery/{id}/status.
        "open_link":     r.get("cover_link") or None,
        "error_reason":  r.get("error_reason") or "",
    }

    # Best-effort: extract the numeric gallery_id from the URL so the
    # frontend can build a fallback deep-link (still needs an extra RTT).
    url = out["url"] or ""
    if url and HAVE_GS:
        try:
            out["gallery_id"] = _gs.extract_gallery_id(url)
        except Exception:
            pass
    return out
