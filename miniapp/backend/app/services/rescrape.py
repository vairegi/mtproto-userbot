"""
rescrape.py — Force Re-scrape service (admin escape hatch).

Provides two admin-facing helpers:

  1. list_failed_galleries(limit) — returns detailed rows for every
     gallery in a FAILED_* / PARTIAL state, so the admin panel can show
     WHY the gallery failed and offer a per-row "Force Re-scrape" button.

  2. force_rescrape(url_or_id) — the actual re-scrape trigger:
        * resets the galleries doc via gallery_state.reset_doc()
        * clears any completed / pending / failed queue rows for the same
          url_hash so queue_service.enqueue_batch can accept the URL again
        * enqueues a fresh job through queue_service.enqueue_batch
        * returns a dict with the new job_id or a clear reason for failure.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any, Dict, List, Optional

log = logging.getLogger("miniapp.rescrape")

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in [
    os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..")),
    "/opt/render/project/src",
]:
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    try:  # v12.53: deterministic repo-root db load
        from ..rootdb import load as _lrd
    except ImportError:  # services imported as top-level package
        from rootdb import load as _lrd
    _bot_db = _lrd()
    import gallery_state as _gs         # type: ignore
    import queue_service as _qs         # type: ignore
    from url_utils import parse_batch   # type: ignore
    HAVE_BOT = True
except Exception as e:  # noqa: BLE001
    _bot_db = None
    _gs = None
    _qs = None
    parse_batch = None  # type: ignore
    HAVE_BOT = False
    log.warning("rescrape: bot helpers not importable — service disabled (%s)", e)


_FAILED_STATUSES = (
    "FAILED_TIMEOUT",
    "FAILED_BOT2_ERROR",
    "FAILED_SCRAPE",
    "FAILED_OTHER",
    "FAILED_RECOVERED",
    "PARTIAL",
)


def _iso(v: Any) -> Optional[str]:
    try:
        import datetime as _dt
        if isinstance(v, _dt.datetime):
            return v.isoformat()
        if isinstance(v, (int, float)):
            return _dt.datetime.utcfromtimestamp(float(v)).isoformat() + "Z"
    except Exception:
        return None
    return None


def _reconstruct_url(gid: Any) -> str:
    try:
        return f"https://nhentai.net/g/{int(str(gid))}/"
    except Exception:
        return ""


def list_failed_galleries(limit: int = 50) -> List[Dict[str, Any]]:
    """Return up to `limit` galleries in a FAILED_* / PARTIAL state,
    newest first, with the details an admin needs to decide whether to
    re-scrape."""
    if not HAVE_BOT:
        return []
    conn = _bot_db.connect()
    try:
        cur = (conn.galleries
               .find({"status": {"$in": list(_FAILED_STATUSES)}})
               .sort("updated_at", -1)
               .limit(int(max(1, min(500, limit)))))
        out: List[Dict[str, Any]] = []
        for doc in cur:
            out.append({
                "gallery_id":     str(doc.get("_id") or ""),
                "title":          doc.get("title") or "",
                "status":         doc.get("status") or "",
                "failed_reason":  doc.get("failed_reason") or "",
                "updated_at":     _iso(doc.get("updated_at")),
                "created_at":     _iso(doc.get("created_at")),
                "url":            doc.get("url") or _reconstruct_url(doc.get("_id")),
                "pages":          doc.get("pages"),
                "open_link":      doc.get("open_link") or "",
                "requester":      doc.get("requester") or doc.get("submitted_by") or "",
            })
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("list_failed_galleries: query failed: %s", e)
        return []
    finally:
        try: conn.close()
        except Exception: pass


def _purge_queue_rows(url: str) -> Dict[str, Any]:
    """Delete every queue row that shares the same url_hash as `url`.

    v11.5: ALSO clears the processed_urls tombstone. The queue_service dedup
    gate reads processed_urls.completed_at to decide "already done"; leaving
    it behind after a Force Re-scrape was the reason Force Re-scrape used to
    fail silently (enqueue_batch reported skipped_already_done and produced
    no queued rows).
    """
    if not parse_batch:
        return {"purged": 0, "reason": "url_utils unavailable"}
    parsed = parse_batch(url, max_links=1)
    if not parsed.accepted:
        return {"purged": 0, "reason": "url did not parse"}
    p = parsed.accepted[0]
    conn = _bot_db.connect()
    try:
        rq = conn.queue.delete_many({"url_hash": p.url_hash})
        pu_n = 0
        try:
            r_pu = conn.processed_urls.delete_many({"_id": p.url_hash})
            pu_n += int(r_pu.deleted_count)
            r_pu2 = conn.processed_urls.delete_many({"url": p.normalised})
            pu_n += int(r_pu2.deleted_count)
        except Exception as e:  # noqa: BLE001
            log.warning("purge: processed_urls delete failed: %s", e)
            pu_n = -1
        return {
            "purged": int(rq.deleted_count),
            "processed_urls_purged": pu_n,
            "url_hash": p.url_hash,
        }
    finally:
        try: conn.close()
        except Exception: pass


def _first_reason(er: Any) -> str:
    if not er:
        return ""
    if getattr(er, "rejected", None):
        return "rejected: " + "; ".join(f"{u} ({w})" for u, w in er.rejected[:3])
    if getattr(er, "skipped_already_pending", None):
        return "already pending/processing: " + er.skipped_already_pending[0]
    if getattr(er, "skipped_already_done", None):
        return ("already completed (dedup gate); "
                "reset failed to purge it — try again")
    if getattr(er, "skipped_duplicates", None):
        return "duplicate within submission"
    return ""


def force_rescrape(
    url_or_id: str,
    *,
    submitted_by: Optional[int] = None,
    username: Optional[str] = None,
) -> Dict[str, Any]:
    """Reset the galleries doc + purge lingering queue rows + re-enqueue."""
    if not HAVE_BOT:
        return {"ok": False, "reason": "bot helpers unavailable"}

    raw = (url_or_id or "").strip()
    if not raw:
        return {"ok": False, "reason": "empty URL / gallery id"}

    url = f"https://nhentai.net/g/{raw}/" if raw.isdigit() else raw

    try:
        gid = _gs.extract_gallery_id(url) if _gs else None
    except Exception:
        gid = None

    reset_ok = False
    if gid:
        try:
            conn = _bot_db.connect()
            try:
                reset_ok = bool(_gs.reset_doc(conn, gid))
            finally:
                try: conn.close()
                except Exception: pass
        except Exception as e:  # noqa: BLE001
            log.warning("rescrape: reset_doc(%s) raised: %s", gid, e)

    try:
        purge_result = _purge_queue_rows(url)
    except Exception as e:  # noqa: BLE001
        log.warning("rescrape: _purge_queue_rows raised: %s", e)
        purge_result = {"purged": 0, "error": str(e)}

    try:
        er = _qs.enqueue_batch(
            url,
            max_links=1,
            via_search=False,
            submitted_by=int(submitted_by) if submitted_by else None,
            username=(username or "admin_rescrape"),
            chat_id=None,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"enqueue raised: {e}",
                "reset": reset_ok, "purge": purge_result}

    if er and er.queued:
        job_id, gallery_url = er.queued[0]
        return {"ok": True, "job_id": int(job_id), "url": gallery_url,
                "reset": reset_ok, "purge": purge_result}

    reason = _first_reason(er)
    return {"ok": False,
            "reason": reason or "enqueue_batch produced no queued rows",
            "reset": reset_ok, "purge": purge_result,
            "rejected":                list(getattr(er, "rejected", []) or []),
            "skipped_duplicates":      list(getattr(er, "skipped_duplicates", []) or []),
            "skipped_already_pending": list(getattr(er, "skipped_already_pending", []) or []),
            "skipped_already_done":    list(getattr(er, "skipped_already_done", []) or [])}


def _summarise_doc(g: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not g: return None
    return {
        "status":         g.get("status"),
        "failed_reason":  g.get("failed_reason") or "",
        "title":          g.get("title") or "",
        "updated_at":     _iso(g.get("updated_at")),
    }


def _summarise_qrow(r: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not r: return None
    return {
        "id":            r.get("_id"),
        "status":        r.get("status"),
        "error_reason":  r.get("error_reason") or "",
        "created_at":    _iso(r.get("created_at")),
        "updated_at":    _iso(r.get("updated_at")),
    }


def diagnose(url_or_id: str) -> Dict[str, Any]:
    """Non-destructive lookup: current galleries doc + lingering queue rows."""
    if not HAVE_BOT:
        return {"ok": False, "reason": "bot helpers unavailable"}
    raw = (url_or_id or "").strip()
    if not raw:
        return {"ok": False, "reason": "empty URL / gallery id"}
    url = f"https://nhentai.net/g/{raw}/" if raw.isdigit() else raw

    gid = None
    try:
        gid = _gs.extract_gallery_id(url) if _gs else None
    except Exception:
        pass

    out: Dict[str, Any] = {"ok": True, "url": url, "gallery_id": gid}
    conn = _bot_db.connect()
    try:
        if gid:
            g = conn.galleries.find_one({"_id": str(gid)})
            out["gallery_doc"] = _summarise_doc(g)
        if parse_batch:
            parsed = parse_batch(url, max_links=1)
            if parsed.accepted:
                p = parsed.accepted[0]
                out["url_hash"] = p.url_hash
                q = list(conn.queue.find({"url_hash": p.url_hash})
                                     .sort("_id", -1).limit(5))
                out["queue_rows"] = [_summarise_qrow(r) for r in q]
    finally:
        try: conn.close()
        except Exception: pass
    return out


# ---------------------------------------------------------------------------
# v11.3 — Force-Delete (purge WITHOUT re-enqueue)
# ---------------------------------------------------------------------------
#
# The existing force_rescrape() always re-enqueues the URL after purging,
# which is not what an admin wants when they just need a stuck gallery
# GONE from the database (e.g. duplicate processing bugs, a poisoned
# doc that keeps claiming PROCESSING slots, or a takedown). purge_gallery
# removes every trace of the gallery from MongoDB:
#
#   * galleries        — the dedup / status doc (via _id = gallery_id)
#   * queue            — every row whose url_hash matches the gallery URL
#   * progress_events  — live-progress rows so stale UI entries disappear
#   * metrics_events   — any pipeline metrics rows referencing the gid
#                        (best-effort; skipped if the collection doesn't
#                        have a gallery_id field)
#
# Returns a dict describing what was deleted so the admin panel can show
# it. This is a HARD delete — there is no tombstone. After purge, the
# next enqueue of the same URL behaves as a completely fresh job.


def purge_gallery(gallery_id: str) -> Dict[str, Any]:
    """Hard-delete a gallery from MongoDB by numeric id (e.g. "650361").

    Returns {"ok": True, "deleted": {...}} on success, or
    {"ok": False, "reason": ...} on failure.
    """
    if not HAVE_BOT:
        return {"ok": False, "reason": "bot helpers unavailable"}

    gid_raw = (gallery_id or "").strip()
    if not gid_raw:
        return {"ok": False, "reason": "empty gallery id"}
    if not gid_raw.isdigit():
        return {"ok": False, "reason": "gallery id must be numeric (e.g. 650361)"}

    url = f"https://nhentai.net/g/{gid_raw}/"
    deleted: Dict[str, int] = {}

    conn = _bot_db.connect()
    try:
        # 1) galleries doc (the dedup gate) ---------------------------------
        try:
            r = conn.galleries.delete_one({"_id": str(gid_raw)})
            deleted["galleries"] = int(r.deleted_count)
        except Exception as e:  # noqa: BLE001
            log.warning("purge: galleries delete_one failed: %s", e)
            deleted["galleries"] = -1

        # 2) queue rows (pending / processing / completed / failed) ---------
        #    Also purges processed_urls (v11.5 fix): the queue_service dedup
        #    gate reads processed_urls.completed_at to decide "already done",
        #    so leaving that tombstone behind is why Force-Delete used to
        #    silently swallow the next enqueue attempt.
        url_hash = None
        if parse_batch:
            try:
                parsed = parse_batch(url, max_links=1)
                if parsed.accepted:
                    p = parsed.accepted[0]
                    url_hash = p.url_hash
                    r = conn.queue.delete_many({"url_hash": url_hash})
                    deleted["queue"] = int(r.deleted_count)
            except Exception as e:  # noqa: BLE001
                log.warning("purge: queue delete_many failed: %s", e)
                deleted["queue"] = -1

        # 2b) processed_urls tombstone (v11.5) ------------------------------
        #     _id == url_hash; delete by hash if we have it, and also do a
        #     belt-and-braces delete by url string for older rows that were
        #     keyed differently.
        try:
            n = 0
            if url_hash:
                r = conn.processed_urls.delete_many({"_id": url_hash})
                n += int(r.deleted_count)
            r = conn.processed_urls.delete_many({"url": url})
            n += int(r.deleted_count)
            deleted["processed_urls"] = n
        except Exception as e:  # noqa: BLE001
            log.warning("purge: processed_urls delete failed: %s", e)
            deleted["processed_urls"] = -1

        # 3) progress events (live UI cards) --------------------------------
        try:
            r = conn.db["progress_events"].delete_many(
                {"gallery_id": str(gid_raw)})
            deleted["progress_events"] = int(r.deleted_count)
        except Exception as e:  # noqa: BLE001
            log.warning("purge: progress_events delete failed: %s", e)
            deleted["progress_events"] = -1

        # 4) metrics events (best-effort; field may not exist) --------------
        try:
            r = conn.db["metrics_events"].delete_many(
                {"gallery_id": str(gid_raw)})
            deleted["metrics_events"] = int(r.deleted_count)
        except Exception:  # noqa: BLE001
            # Collection may not have this field — not an error.
            deleted["metrics_events"] = 0
    finally:
        try:
            conn.close()
        except Exception:
            pass

    total = sum(v for v in deleted.values() if isinstance(v, int) and v > 0)
    log.info("purge_gallery(%s) deleted=%s", gid_raw, deleted)
    return {
        "ok": True,
        "gallery_id": gid_raw,
        "url": url,
        "deleted": deleted,
        "total_deleted": total,
        "note": "gallery fully purged — next enqueue will be a fresh job",
    }
