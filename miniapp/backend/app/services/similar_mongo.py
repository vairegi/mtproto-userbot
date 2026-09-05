"""
similar_mongo.py — v12.60: "Similar to this" engine running on Mongo-2.

Mongo doesn't bill per-read, so the full-corpus scoring that was a Turso
disaster (all ~17k gallery rows per request) is free here. Same weighted
scoring as similar_sql.py: +10 per artist/parody/group/character match,
+2 per content-tag match. Fail-open: any error -> [].

Enabled by default (SIMILAR_ENABLED != "0"). Reads the turso_nhentai_cache
collection in the Mongo-2 mirror DB via the shared mongo2_client.
"""
from __future__ import annotations

import json
import time
import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("miniapp.similar_mongo")

_WEIGHT_HEAVY = 10   # artist / parody / group / character
_WEIGHT_TAG = 2      # content tags
_MAX_SCAN = 25000    # hard cap on docs scored per request
# v12.62: per-gallery result cache in Mongo-2 (writes are free here —
# the v12.56 "no Turso similar cache" rationale does not apply to Mongo).
# First open of a gallery pays the scan (~10-15s on M0); every repeat
# open within the TTL is one find_one (~50ms).
_SIM_CACHE_TTL_SEC = int(os.getenv("SIMILAR_CACHE_TTL_SEC", "1800") or 1800)


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


def similar_galleries(gid: str, limit: int = 6) -> List[Dict[str, Any]]:
    """Score every gallery in the Mongo-2 mirror against gallery `gid`."""
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
    try:
        tp = json.loads(target["payload"]) if isinstance(target["payload"], str) else target["payload"]
    except Exception:
        return []
    sig = _tags_of(tp)
    if not sig["heavy"] and not sig["tags"]:
        return []

    heavy_set, tag_set = set(sig["heavy"]), set(sig["tags"])

    # 2) bounded corpus scan + score in Python (Mongo-side regex prefilter
    #    would need unwound arrays we don't store; plain scan is fine —
    #    no per-read billing on Mongo).
    scored = []
    cursor = coll.find(
        {"_id": {"$regex": "^gallery:", "$ne": f"gallery:{gid}"}},
        {"payload": 1},
    ).limit(_MAX_SCAN)
    n_scanned = 0
    for doc in cursor:
        n_scanned += 1
        try:
            p = doc.get("payload")
            p = json.loads(p) if isinstance(p, str) else p
            s2 = _tags_of(p)
            score = _WEIGHT_HEAVY * len(heavy_set & set(s2["heavy"])) \
                    + _WEIGHT_TAG * len(tag_set & set(s2["tags"]))
            if score > 0:
                scored.append((score, p))
        except Exception:  # noqa: BLE001
            continue
    scored.sort(key=lambda t: -t[0])
    out = []
    for score, p in scored[:limit]:
        card = {
            "id": p.get("id"), "title": p.get("title") or p.get("title_en_clean"),
            "cover": p.get("cover"), "pages": p.get("pages"),
            "favorites": p.get("favorites", 0),
            "tags": p.get("tags") or [],
            "score": score,
        }
        if card["id"] is not None:
            out.append(card)
    log.info("📖 /similar gid=%s source=MONGO2 scored=%d rows -> %d cards",
             gid, n_scanned, len(out))
    # v12.62: cache the result for repeat opens (best-effort)
    try:
        coll.replace_one({"_id": _ck},
                         {"_id": _ck, "items": out, "cached_at": time.time()},
                         upsert=True)
    except Exception:  # noqa: BLE001
        pass
    return out
