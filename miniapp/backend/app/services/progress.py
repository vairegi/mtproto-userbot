"""
progress.py — Live-progress lookup service.

The mini-app polls GET /api/queue/progress/<gallery_id> while a gallery
is being downloaded so the detail sheet can show a live "Your PDF is
being generated…" card. This service reads from the SAME collections the
worker + relay_v2 update, so no writer changes are required.

Data sources (read-only):
  * `galleries` (V2 dedup gate) — canonical status for known galleries.
  * `queue`     (bot's job table) — fallback for jobs where V2 hasn't
                                    flipped a status yet (fresh enqueue
                                    → still pending), and for the
                                    `error_reason` on failed rows.
  * `progress_events` (optional) — if progress_tracker.py has already
                                    started writing granular percentage
                                    events, we surface the latest one so
                                    the UI can show "45% — 12/26 pages".
                                    Absent → we fall back to a coarse
                                    status label.

The returned dict is intentionally small + JSON-friendly so it's cheap
to poll at 2-3s intervals.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Dict, Optional

log = logging.getLogger("miniapp.progress")

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
    HAVE_BOT = True
except Exception as e:  # noqa: BLE001
    _bot_db = None
    _gs = None
    HAVE_BOT = False
    log.warning("progress: bot helpers not importable — service disabled (%s)", e)


# Coarse "human status" the frontend uses when we don't have a pct.
_HUMAN = {
    "PROCESSING":         "Your PDF is being generated…",
    "PENDING":             "Queued — waiting for a worker…",
    "COMPLETED":           "Ready — delivering to your DM…",
    "PARTIAL":             "Delivered (cover posted, PDF partial)",
    "FAILED_TIMEOUT":      "Failed: worker timed out",
    "FAILED_BOT2_ERROR":   "Failed: Bot 2 refused",
    "FAILED_BOT2":         "Both backup bots failed — retry available later",
    "FAILED_BOT2":         "Both backup bots failed — retry available later",
    "FAILED_SCRAPE":       "Failed: could not scrape gallery",
    "FAILED_OTHER":        "Failed: unknown error",
    "FAILED_RECOVERED":    "Failed earlier — retry queued",
}

# v12.72: human labels for Bot 2's live progress.stage sub-doc.
_STAGE_HUMAN = {
    "fetching":          "Contacting @Gallery_DLBot…",
    "downloading":       "Downloading pages…",
    "fallback_fetching": "Handed to backup bot — downloading…",
    "compiling":         "Compiling PDF…",
    "uploading":         "Uploading to database channel…",
}

# Both-bots park (matches Bot 2 default; env override on that side).
_BOTH_FAIL_PARK_S = 43200

# v12.72: human labels for Bot 2's live progress.stage sub-doc.
_STAGE_HUMAN = {
    "fetching":          "Contacting @Gallery_DLBot…",
    "downloading":       "Downloading pages…",
    "fallback_fetching": "Handed to backup bot — downloading…",
    "compiling":         "Compiling PDF…",
    "uploading":         "Uploading to database channel…",
}

# Both-bots park (matches Bot 2 default; env override on that side).
_BOTH_FAIL_PARK_S = 43200


def _latest_progress_event(conn, gid: str) -> Optional[Dict[str, Any]]:
    """Return the most recent progress_events row for this gallery, or
    None if the collection is empty / absent."""
    try:
        col = conn.db["progress_events"]
    except Exception:
        return None
    try:
        cur = col.find({"gallery_id": str(gid)}).sort("ts", -1).limit(1)
        for row in cur:
            return {
                "pct":    int(row.get("pct") or 0),
                "phase":  str(row.get("phase") or ""),
                "detail": str(row.get("detail") or ""),
                "ts":     row.get("ts"),
            }
    except Exception:
        return None
    return None


def _latest_queue_row(conn, url_hash_or_id: str) -> Optional[Dict[str, Any]]:
    """Best-effort lookup of the most recent queue row matching either
    a numeric gallery_id or a url_hash. Returns None if nothing found."""
    try:
        col = conn.queue
    except Exception:
        return None
    q = {}
    s = str(url_hash_or_id or "")
    if s.isdigit():
        q = {"$or": [
            {"gallery_id": s},
            {"url": {"$regex": f"/g/{s}/?"}},
        ]}
    else:
        q = {"url_hash": s}
    try:
        cur = col.find(q).sort("_id", -1).limit(1)
        for row in cur:
            return row
    except Exception:
        return None
    return None


def lookup(url_or_id: str) -> Dict[str, Any]:
    """Public entry point. Returns a compact live-progress payload the
    frontend can render directly. Never raises."""
    if not HAVE_BOT:
        return {"ok": False, "known": False, "reason": "backend unavailable"}

    raw = (url_or_id or "").strip().lstrip("#")
    if not raw:
        return {"ok": False, "known": False, "reason": "empty id"}

    gid: Optional[str] = None
    try:
        if _gs:
            gid = _gs.extract_gallery_id(raw) or (raw if raw.isdigit() else None)
    except Exception:
        gid = raw if raw.isdigit() else None

    conn = _bot_db.connect()
    try:
        doc = None
        if gid:
            try:
                doc = _gs.get(conn, gid) if _gs else None
            except Exception:
                doc = None

        # --- Path A: known in V2 galleries -----------------------------
        if doc:
            status = str(doc.get("status") or "").upper()
            ev = _latest_progress_event(conn, gid) if gid else None
            out: Dict[str, Any] = {
                "ok":          True,
                "known":       True,
                "gallery_id":  gid,
                "status":      status,
                "human":       _HUMAN.get(status, status),
                "title":       doc.get("title") or "",
                "pages":       doc.get("pages"),
                "open_link":   doc.get("open_link") or "",
                "failed_reason": doc.get("failed_reason") or "",
                "updated_at":  _epoch(doc.get("updated_at")),
                "is_active":   status in ("PROCESSING", "PENDING"),
                "is_failed":   status.startswith("FAILED"),
                "is_done":     status in ("COMPLETED", "PARTIAL"),
            }
            if ev:
                out["pct"]         = ev["pct"]
                out["phase"]       = ev["phase"]
                out["detail"]      = ev["detail"]
                out["last_event_ts"] = _epoch(ev.get("ts"))
            # v12.72: surface Bot 2's live progress.* sub-doc. Additive —
            # older frontends that don't know these keys keep working.
            prog = doc.get("progress") if isinstance(doc, dict) else None
            if isinstance(prog, dict):
                stage = str(prog.get("stage") or "").strip()
                if stage:
                    out["stage"]        = stage
                    out["stage_human"]  = _STAGE_HUMAN.get(stage, stage)
                    if prog.get("page") is not None:
                        try: out["page"] = int(prog.get("page"))
                        except (TypeError, ValueError): pass
                    if prog.get("total") is not None:
                        try: out["total"] = int(prog.get("total"))
                        except (TypeError, ValueError): pass
                    if prog.get("eta_s") is not None:
                        try: out["eta_s"] = int(prog.get("eta_s"))
                        except (TypeError, ValueError): pass
                    out["progress_updated_at"] = _epoch(prog.get("updated_at"))
            # v12.72: FAILED_BOT2 park countdown (see mongo_state
            # set_both_failed). Frontend disables Retry until it hits 0.
            if status == "FAILED_BOT2":
                try:
                    ref = 0.0
                    for k in ("both_failed_at", "failed_at",
                              "completed_at", "updated_at"):
                        v = _epoch(doc.get(k)) or 0.0
                        if v > ref: ref = v
                    park = float(doc.get("both_fail_park_s") or _BOTH_FAIL_PARK_S)
                    if ref > 0 and park > 0:
                        remain = int(ref + park - time.time())
                        out["retry_available_in_s"] = max(0, remain)
                except Exception:
                    pass
            return out

        # --- Path B: fall back to the queue table ----------------------
        qrow = _latest_queue_row(conn, gid or raw)
        if not qrow:
            return {"ok": True, "known": False, "gallery_id": gid,
                    "human": "No active download for this gallery.",
                    "is_active": False, "is_failed": False, "is_done": False}

        status = str(qrow.get("status") or "").upper()
        return {
            "ok":            True,
            "known":         True,
            "gallery_id":    gid,
            "status":        status,
            "human":         _HUMAN.get(status, status or "Queued…"),
            "title":         qrow.get("title") or "",
            "failed_reason": qrow.get("error_reason") or "",
            "updated_at":    _epoch(qrow.get("updated_at")),
            "is_active":     status in ("PENDING", "PROCESSING"),
            "is_failed":     status.startswith("FAILED") or status == "FAILED",
            "is_done":       status == "COMPLETED",
            "source":        "queue",
        }
    finally:
        try: conn.close()
        except Exception: pass


def _epoch(v: Any) -> Optional[float]:
    """Coerce datetime | float | None to a float epoch (or None)."""
    try:
        import datetime as _dt
        if isinstance(v, _dt.datetime):
            return v.timestamp()
        if isinstance(v, (int, float)):
            return float(v)
    except Exception:
        return None
    return None


def epoch_now() -> float:
    return time.time()
