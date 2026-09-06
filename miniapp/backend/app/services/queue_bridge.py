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
    try:  # v12.53: deterministic repo-root db load
        from ..rootdb import load as _lrd
    except ImportError:  # services imported as top-level package
        from rootdb import load as _lrd
    _bot_db = _lrd()
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


class EnqueueEmptyResult(RuntimeError):
    """v11.5 — raised when queue_service returns no queued rows for the URL.

    Carries the raw skipped/rejected diagnostics from the EnqueueResult so
    the /api/queue route can surface them to admins without them having to
    scrape server logs.
    """
    def __init__(self, msg: str, *,
                 skipped_already_done: list | None = None,
                 skipped_already_pending: list | None = None,
                 skipped_duplicates: list | None = None,
                 rejected: list | None = None):
        super().__init__(msg)
        self.skipped_already_done    = list(skipped_already_done or [])
        self.skipped_already_pending = list(skipped_already_pending or [])
        self.skipped_duplicates      = list(skipped_duplicates or [])
        self.rejected                = list(rejected or [])


# v12.72: process-local single-flight guard. When two users (or a
# double-tap) POST /api/queue for the same URL within a couple of
# seconds, we return the SAME already-in-flight result to the second
# caller instead of double-writing. Bot 2's Mongo CAS is still the real
# safety net; this just cuts the window down at the door.
import threading as _sf_threading
import time as _sf_time
_SF_TTL_S = 3.0
_sf_lock = _sf_threading.Lock()
_sf_inflight: dict = {}  # url -> (expires_at, result_dict)


def _sf_get(url: str):
    now = _sf_time.time()
    with _sf_lock:
        row = _sf_inflight.get(url)
        if row and row[0] > now:
            return row[1]
        # opportunistic cleanup
        for k in [k for k, v in _sf_inflight.items() if v[0] <= now]:
            _sf_inflight.pop(k, None)
    return None


def _sf_put(url: str, result: dict):
    with _sf_lock:
        _sf_inflight[url] = (_sf_time.time() + _SF_TTL_S, dict(result))


def enqueue(url: str, user_id: int, username: str | None) -> dict:
    if not HAVE_BOT:
        raise RuntimeError("queue_service not available in this deployment")
    # v12.72: single-flight coalesce for concurrent identical requests.
    dup = _sf_get(url)
    if dup is not None:
        return dict(dup)
    result = _qs.enqueue_batch(
        url,
        max_links=1,
        via_search=False,
        submitted_by=int(user_id),
        username=(username or "miniapp"),
        chat_id=None,
    )
    if not result or not getattr(result, "queued", None):
        raise EnqueueEmptyResult(
            "enqueue_batch returned nothing",
            skipped_already_done=list(getattr(result, "skipped_already_done", []) or []),
            skipped_already_pending=list(getattr(result, "skipped_already_pending", []) or []),
            skipped_duplicates=list(getattr(result, "skipped_duplicates", []) or []),
            rejected=list(getattr(result, "rejected", []) or []),
        )
    job_id, gallery_url = result.queued[0][0], result.queued[0][1]
    out = {"job_id": job_id, "url": gallery_url}
    _sf_put(url, out)
    return out


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
            "workers": _workers(conn),
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


# ------------------------------------------------------------------ v12.76
# Live per-worker view for the Queue tab. ONE indexed query on the
# galleries collection (status index exists since v12.x), projection
# only the fields we render, hard cap 4 rows, at most 2 returned to the
# frontend. All failures degrade to [] so the status endpoint never 500s.
_WORKER_STAGE_HUMAN = {
    "fetching":          "Contacting download bot…",
    "downloading":       "Downloading pages",
    "fallback_fetching": "Backup bot downloading",
    "compiling":         "Compiling PDF…",
    "uploading":         "Uploading to database channel…",
}
_WORKER_STAGE_PCT = {"fetching": 8, "fallback_fetching": 30,
                     "compiling": 90, "uploading": 96}


def _worker_pct(stage: str, page: Any, total: Any) -> int:
    """Monotonic pseudo-percent. Downloading interpolates 8..85 by
    page/total; other stages use fixed anchors so the bar never jumps
    backwards when stage flips."""
    try:
        page = int(page or 0)
        total = int(total or 0)
    except (TypeError, ValueError):
        page, total = 0, 0
    if stage == "downloading":
        if page > 0 and total > 0:
            return max(8, min(85, 8 + int(77 * page / max(total, 1))))
        return 15
    return _WORKER_STAGE_PCT.get(stage, 10)


def _workers(conn) -> list:
    try:
        coll = getattr(conn, "galleries", None)
        if coll is None:
            return []
        cur = (coll.find(
                   {"status": "PROCESSING"},
                   projection={"title": 1, "pages": 1, "started_at": 1,
                               "progress": 1})
               .sort("started_at", 1)
               .limit(4))
        out = []
        for doc in cur:
            try:
                prog = doc.get("progress") or {}
                if not isinstance(prog, dict):
                    prog = {}
                stage = str(prog.get("stage") or "")
                try:
                    slot = int(prog.get("slot")) if prog.get("slot") is not None else None
                except (TypeError, ValueError):
                    slot = None
                page = prog.get("page")
                total = prog.get("total") or doc.get("pages")
                out.append({
                    "gid": str(doc.get("_id") or ""),
                    "slot": slot,
                    "title": (doc.get("title") or "")[:80],
                    "stage": stage,
                    "stage_human": _WORKER_STAGE_HUMAN.get(stage, "Working…"),
                    "page": page,
                    "total": total,
                    "pct": _worker_pct(stage, page, total),
                })
                if len(out) >= 2:
                    break
            except Exception:
                continue
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("queue workers lookup failed (degrading to []): %s", e)
        return []
