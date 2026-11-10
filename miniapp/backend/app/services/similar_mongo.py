"""
similar_mongo.py — v13.16: bounded signal snapshots and compact cards.

The scraper writes Mongo-1 nhentai_cache. Read and cache results there first;
only try the older Mongo-2 mirror on a primary miss/error. Retains the bounded
heap, scan cap and admission semaphore introduced by the OOM fixes below.

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


# One compact, short-lived signal snapshot per process, not one per gallery.
# Corpus reads are Mongo -> Render ingress; full gallery payloads are never
# read for the normal primary signal scan. Legacy JSON needs a bounded fallback.
_SIGNAL_INDEX = {}
_SIGNAL_TTL_SEC = 600


def _collection_key(coll):
    try:
        return (id(coll.database.client), coll.full_name)
    except AttributeError:
        return (id(coll), "legacy-test")


def _signal_rows(coll):
    key, now = _collection_key(coll), time.monotonic()
    if (_SIGNAL_INDEX.get("key") == key
            and now - _SIGNAL_INDEX.get("built", 0) < _SIGNAL_TTL_SEC):
        return _SIGNAL_INDEX["rows"], False
    rows, seen, pool = [], set(), {}
    consumed = 0
    def keep(gid, payload):
        if not gid.isdigit() or gid in seen:
            return
        seen.add(gid)
        sig = _tags_of(payload)
        if not sig["heavy"] and not sig["tags"]:
            return
        # Intern names within this snapshot to avoid storing repeated tag
        # strings for every gallery on a 512MB host.
        h = frozenset(pool.setdefault(x, x) for x in sig["heavy"])
        t = frozenset(pool.setdefault(x, x) for x in sig["tags"])
        rows.append((gid, h, t))
    # Bot 1 stores a BSON object. Project just scoring fields: never
    # pages_meta, covers, titles, or the other full gallery metadata.
    cursor = coll.find({"_id": {"$regex": "^gallery:"},
                        "payload": {"$type": "object"}},
                       {"payload.tag_groups": 1, "payload.tags": 1}).limit(_MAX_SCAN)
    try:
        for doc in cursor:
            consumed += 1
            payload = doc.get("payload")
            if isinstance(payload, dict):
                keep(str(doc.get("_id", "")).split(":", 1)[-1], payload)
    finally:
        if hasattr(cursor, "close"): cursor.close()
    # Existing mirrors may hold JSON strings. Mongo cannot project inside
    # them; parse once per snapshot, discard full payload immediately.
    remaining = _MAX_SCAN - consumed
    if remaining > 0:
        cursor = coll.find({"_id": {"$regex": "^gallery:"},
                            "payload": {"$type": "string"}}, {"payload": 1}).limit(remaining)
        try:
            for doc in cursor:
                payload = _load_payload(doc.get("payload"))
                if isinstance(payload, dict):
                    keep(str(doc.get("_id", "")).split(":", 1)[-1], payload)
        finally:
            if hasattr(cursor, "close"): cursor.close()
    _SIGNAL_INDEX.clear()  # one corpus at a time: bounded process RAM
    _SIGNAL_INDEX.update(key=key, built=time.monotonic(), rows=rows)
    return rows, True


def _scan_and_score(coll, gid: str, heavy_set: set, tag_set: set,
                    limit: int) -> tuple:
    heap = []
    rows, refreshed = _signal_rows(coll)
    for g, heavy, tags in rows:
        if g == gid:
            continue
        score = (_WEIGHT_HEAVY * len(heavy_set & heavy)
                 + _WEIGHT_TAG * len(tag_set & tags))
        if score <= 0:
            continue
        if len(heap) < limit:
            heapq.heappush(heap, (score, g))
        elif score > heap[0][0]:
            heapq.heapreplace(heap, (score, g))
    log.info("similar signals: %s rows=%d (metadata-only primary projection)",
             "refreshed" if refreshed else "reused", len(rows))
    return heap, len(rows)


def similar_galleries(gid: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Score the live primary corpus, then legacy Mongo-2. Fail-open -> []."""
    if not _enabled():
        log.warning("similar_mongo: disabled via SIMILAR_ENABLED=0")
        return []
    gid = str(gid or "").strip()
    if not gid.isdigit():
        return []
    limit = max(1, min(int(limit or 6), 12))

    # Primary is Bot 1's current live writer. Never use app.db's separate
    # MINIAPP/MONGO_DB namespace here; rootdb honors MONGO_DB_NAME.
    try:
        from .. import rootdb
        root = rootdb.load()
        if root is not None:
            out = _similar_from_collection(root.connect().nhentai_cache,
                                          "MONGO1", gid, limit)
            if out:
                return out
    except Exception as exc:
        log.warning("similar_mongo: primary unavailable (%s)", type(exc).__name__)
    # Lazy fallback: a primary hit must not wait for a cold/unreachable
    # legacy connection or build its indexes on the user-facing hot path.
    try:
        from common import mongo2_client as m2
        if m2.reads_enabled():
            legacy = m2._get_coll()
            if legacy is not None:
                return _similar_from_collection(legacy, "MONGO2", gid, limit)
    except Exception as exc:
        log.warning("similar_mongo(%s): legacy unavailable (%s)",
                    gid, type(exc).__name__)
    return []


def _similar_from_collection(coll, source: str, gid: str, limit: int) -> list:
    # v12.62: result cache hit = instant response, zero scan
    _ck = f"similar:v13.16:{gid}"
    try:
        _hit = coll.find_one({"_id": _ck}, {"items": 1, "cached_at": 1, "requested_limit": 1})
        if _hit and isinstance(_hit.get("items"), list) and _hit["items"]:
            _age = time.time() - float(_hit.get("cached_at") or 0)
            if (0 <= _age < _SIM_CACHE_TTL_SEC
                    and (len(_hit["items"]) >= limit
                         or int(_hit.get("requested_limit") or 0) >= limit)):
                log.info("📖 /similar gid=%s source=%s-CACHE age=%ds -> %d cards",
                         gid, source, int(_age), len(_hit["items"]))
                return _hit["items"][:limit]
    except Exception:  # noqa: BLE001
        pass

    # 1) target signals
    target = coll.find_one({"_id": f"gallery:{gid}", "payload": {"$type": "object"}},
                           {"payload.tag_groups": 1, "payload.tags": 1})
    if not target or not target.get("payload"):
        target = coll.find_one({"_id": f"gallery:{gid}", "payload": {"$type": "string"}},
                               {"payload": 1})
    if not target or not target.get("payload"):
        log.info("similar_mongo(%s): target not in %s; skipping", gid, source)
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
        # A waiter might have been queued behind a request for this same
        # gallery. Re-read the tiny cached result after admission.
        hit = coll.find_one({"_id": _ck}, {"items": 1, "cached_at": 1, "requested_limit": 1})
        if (hit and hit.get("items") and 0 <= time.time() - float(hit.get("cached_at") or 0) < _SIM_CACHE_TTL_SEC
                and int(hit.get("requested_limit") or 0) >= limit):
            return hit["items"][:limit]
        heap, n_scanned = _scan_and_score(coll, gid, heavy_set, tag_set, limit)
        # 3) re-fetch ONLY the winners' payloads (one $in query, ≤limit docs)
        winners = sorted(heap, key=lambda t: -t[0])
        win_keys = [f"gallery:{g}" for _s, g in winners]
        docs_by_id = {}
        if win_keys:
            for d in coll.find({"_id": {"$in": win_keys}, "payload": {"$type": "object"}},
                               {"payload.id": 1, "payload.title": 1, "payload.title_en_clean": 1,
                                "payload.cover": 1, "payload.pages": 1, "payload.favorites": 1}):
                docs_by_id[d.get("_id")] = d
            missing = [k for k in win_keys if k not in docs_by_id]
            for d in (coll.find({"_id": {"$in": missing}, "payload": {"$type": "string"}},
                                {"payload": 1}) if missing else []):
                docs_by_id[d.get("_id")] = d
        out: List[Dict[str, Any]] = []
        for score, g in winners:
            d = docs_by_id.get(f"gallery:{g}")
            p = _load_payload(d.get("payload")) if d else None
            if not isinstance(p, dict):
                continue
            card = {
                "id": p.get("id") or g,
                "title": p.get("title") or p.get("title_en_clean"),
                "cover": p.get("cover"), "pages": p.get("pages"),
                "favorites": p.get("favorites", 0),
                "tags": [],  # card UI fetches full tags only when the user opens it
                "score": score,
            }
            if card["id"] is not None:
                out.append(card)
    finally:
        _SCAN_SLOTS.release()

    log.info("📖 /similar gid=%s source=%s scored=%d signal rows -> %d cards (bounded snapshot)",
             gid, source, n_scanned, len(out))
    # v12.62: cache the result for repeat opens (best-effort)
    try:
        if not out:
            return []  # never poison new discoveries with a cached empty result
        coll.replace_one({"_id": _ck},
                         {"_id": _ck, "items": out, "cached_at": time.time(),
                          "requested_limit": limit},
                         upsert=True)
    except Exception:  # noqa: BLE001
        pass
    return out
