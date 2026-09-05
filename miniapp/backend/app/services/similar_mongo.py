"""
similar_mongo.py — v12.63: "Similar to this" engine on Mongo-2 (OOM-fixed).

History:
  v12.60 — Mongo-native scoring (no per-read billing vs Turso).
  v12.62 — per-gallery result cache (similar:<gid>, 30 min TTL).
  v12.63 — OOM fix. The v12.60 scan accumulated (score, FULL_PAYLOAD) for
           every matching gallery and, because the route is a sync handler,
           2-3 concurrent cold-gallery requests ran scans IN PARALLEL via
           FastAPI's threadpool: 6 scans in 2.5 min, ~150MB each on a 512MB
           Render instance -> "Ran out of memory (used over 512MB)" kill
           loop (2026-09-05). Two structural fixes:
             1. TOP-N HEAP — only (score, gid) tuples are kept during the
                scan; payloads are parsed, scored and RELEASED immediately.
                Winners' payloads are re-fetched with ONE $in query at the
                end. Peak RAM per scan: a few MB instead of ~150MB.
             2. SINGLE-FLIGHT LOCK — one scan at a time per process;
                concurrent requests wait (lean scan ~2-4s) or fail open.
"""
from __future__ import annotations

import heapq
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("miniapp.similar_mongo")

_WEIGHT_HEAVY = 10   # artist / parody / group / character
_WEIGHT_TAG = 2      # content tags
_MAX_SCAN = 25000    # hard cap on docs scored per request
_SCAN_WAIT_SEC = 20.0  # max time a concurrent request waits for the lock

# v12.62: per-gallery result cache in Mongo-2 (writes are free here —
# the v12.56 "no Turso similar cache" rationale does not apply to Mongo).
_SIM_CACHE_TTL_SEC = int(os.getenv("SIMILAR_CACHE_TTL_SEC", "1800") or 1800)

# v12.63: process-wide single-flight lock — prevents N concurrent full
# scans from stacking in RAM on the 512MB instance.
_SCAN_LOCK = threading.Lock()

# v12.65: admission control. The lean scan is ~4s but still transiently
# allocates per doc; three of them landing during an app-open burst on a
# 512MB instance (shared with admin_bot + worker) was the OOM peak in the
# 2026-09-05 11:24 crash. Cap concurrent scans at 1 HARD and make waiters
# beyond the first few fail open instantly instead of queueing memory work.
_SCAN_SLOTS = threading.BoundedSemaphore(int(os.getenv("SIMILAR_MAX_CONCURRENT", "1") or 1))


def _enabled() -> bool:
    return os.getenv("SIMILAR_ENABLED", "1").strip().lower() not in ("0", "false", "off", "no")


def _tags_of(doc_payload: dict) -> Dict[str, List[str]]:
    """Extract scoring signals from a canonical gallery payload."""
    heavy, tags = [], []
    tg = doc_payload.get("tag_groups")
    if isinstance(tg, dict):
        for k in ("artist", "parody", "group", "character"):
            v = tg.get(k)
            if isinstance(v, list):
                heavy.extend(str(x).lower() for x in v if x)
        v = tg.get("tag") or tg.get("tags")
        if isinstance(v, list):
            tags.extend(str(x).lower() for x in v if x)
    # fallback shape: flat tags list of names
    if not heavy and not tags and isinstance(doc_payload.get("tags"), list):
        tags.extend(str(x).lower() for x in doc_payload["tags"] if isinstance(x, str))
    return {"heavy": heavy, "tags": tags}


def _load_payload(raw: Any) -> Optional[dict]:
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001
        return None


def _scan_and_score(coll, gid: str, heavy_set: set, tag_set: set,
                    limit: int) -> tuple:
    """Stream the corpus, keep ONLY a top-N heap of (score, gid). Returns
    (heap, rows_scanned). Payloads are discarded immediately — this is the
    memory fix."""
    heap: List = []
    n_scanned = 0
    cursor = coll.find(
        {"_id": {"$regex": "^gallery:", "$ne": f"gallery:{gid}"}},
        {"payload": 1},
    ).limit(_MAX_SCAN)
    for doc in cursor:
        n_scanned += 1
        p = _load_payload(doc.get("payload"))
        if not isinstance(p, dict):
            continue
        s2 = _tags_of(p)
        score = (_WEIGHT_HEAVY * len(heavy_set & set(s2["heavy"]))
                 + _WEIGHT_TAG * len(tag_set & set(s2["tags"])))
        if score <= 0:
            continue
        g = str(p.get("id") or "").strip()
        if not g or not g.isdigit():
            g = str(doc.get("_id", "")).split(":", 1)[-1]
        if not g.isdigit():
            continue
        if len(heap) < limit:
            heapq.heappush(heap, (score, g))
        elif score > heap[0][0]:
            heapq.heapreplace(heap, (score, g))
        # p (full payload) goes out of scope here — released, not kept.
    return heap, n_scanned


def similar_galleries(gid: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Score the Mongo-2 corpus against gallery `gid`. Fail-open -> []."""
    if not _enabled():
        log.warning("similar_mongo: disabled via SIMILAR_ENABLED=0")
        return []
    gid = str(gid or "").strip()
    if not gid.isdigit():
        return []
    limit = max(1, min(int(limit or 6), 12))

    from common import mongo2_client as m2
    coll = m2._get_coll()
    if coll is None:
        log.warning("similar_mongo: Mongo-2 unavailable")
        return []

    # v12.62: result cache hit = instant response, zero scan
    _ck = f"similar:{gid}"
    try:
        _hit = coll.find_one({"_id": _ck}, {"items": 1, "cached_at": 1})
        if _hit and isinstance(_hit.get("items"), list):
            _age = time.time() - float(_hit.get("cached_at") or 0)
            if _age < _SIM_CACHE_TTL_SEC:
                log.info("📖 /similar gid=%s source=MONGO2-CACHE age=%ds -> %d cards",
                         gid, int(_age), len(_hit["items"]))
                return _hit["items"][:limit]
    except Exception:  # noqa: BLE001
        pass

    # 1) target signals
    target = coll.find_one({"_id": f"gallery:{gid}"}, {"payload": 1})
    if not target or not target.get("payload"):
        log.info("similar_mongo(%s): target not in Mongo-2; skipping", gid)
        return []
    tp = _load_payload(target["payload"])
    if not isinstance(tp, dict):
        return []
    sig = _tags_of(tp)
    if not sig["heavy"] and not sig["tags"]:
        return []
    heavy_set, tag_set = set(sig["heavy"]), set(sig["tags"])

    # 2) v12.63: single-flight — only one corpus scan may run at a time.
    # Concurrent cold-gallery requests wait their turn (scan is a few
    # seconds) instead of stacking 100MB+ each until the instance OOMs.
    if not _SCAN_SLOTS.acquire(timeout=_SCAN_WAIT_SEC):
        log.warning("similar_mongo(%s): scan slots busy >%ss — failing open "
                    "(client keeps skeletons hidden)", gid, _SCAN_WAIT_SEC)
        return []
    try:
        heap, n_scanned = _scan_and_score(coll, gid, heavy_set, tag_set, limit)
        # 3) re-fetch ONLY the winners' payloads (one $in query, ≤limit docs)
        winners = sorted(heap, key=lambda t: -t[0])
        win_keys = [f"gallery:{g}" for _s, g in winners]
        docs_by_id = {}
        if win_keys:
            for d in coll.find({"_id": {"$in": win_keys}}, {"payload": 1}):
                docs_by_id[d.get("_id")] = d
        out: List[Dict[str, Any]] = []
        for score, g in winners:
            d = docs_by_id.get(f"gallery:{g}")
            p = _load_payload(d.get("payload")) if d else None
            if not isinstance(p, dict):
                continue
            card = {
                "id": p.get("id"),
                "title": p.get("title") or p.get("title_en_clean"),
                "cover": p.get("cover"), "pages": p.get("pages"),
                "favorites": p.get("favorites", 0),
                "tags": p.get("tags") or [],
                "score": score,
            }
            if card["id"] is not None:
                out.append(card)
    finally:
        _SCAN_SLOTS.release()

    log.info("📖 /similar gid=%s source=MONGO2 scored=%d rows -> %d cards (lean-scan)",
             gid, n_scanned, len(out))
    # v12.62: cache the result for repeat opens (best-effort)
    try:
        coll.replace_one({"_id": _ck},
                         {"_id": _ck, "items": out, "cached_at": time.time()},
                         upsert=True)
    except Exception:  # noqa: BLE001
        pass
    return out
