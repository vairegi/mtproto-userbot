"""
nhentai_cache.py — v12.2 Mongo-backed cache + shared token bucket.

Two problems this module solves, both from the v12.1 → v12.2 postmortem:

1. "1 user 429s everyone" — nhentai rate-limits by IP, and your backend has
   one outbound IP. Once a user's search chews through the quota, all other
   users' next requests get 429'd too. Fix: cache aggressively so identical
   upstream requests happen at most ONCE across all users for the whole TTL.

2. "Paginator hit 20 upstream pages per search". Fix: a shared Mongo token
   bucket, sized to the anon limits documented in the real openapi.json at
   nhentai.net/api/v2/openapi.json. Every upstream call consumes a token;
   when the bucket runs dry the caller MUST serve from cache (even stale)
   or fail gracefully — no more silent 429 storms.

TTL policy — deliberately long, per the v12.2 conversation:
  * gallery detail   : 30 days   (immutable after upload)
  * search results   :  3 days   (nhentai's popular/date order barely shifts)
  * suggestions      :  3 days   (same reasoning)
  * trending/homepage: 30 minutes (this one actually rotates)

Everything is best-effort: a Mongo outage MUST NOT break the mini-app.
Every public function catches PyMongoError and returns None / False so the
caller can fall through to the live upstream path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("miniapp.nhentai_cache")

# ---------------------------------------------------------------------------
# TTL config (seconds). Env-overridable so ops can tune without a redeploy.
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, ""))
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


TTL_GALLERY_SEC    = _env_int("NHCACHE_TTL_GALLERY_SEC",    30 * 24 * 3600)   # 30 days
TTL_SEARCH_SEC     = _env_int("NHCACHE_TTL_SEARCH_SEC",      3 * 24 * 3600)   #  3 days
TTL_SUGGEST_SEC    = _env_int("NHCACHE_TTL_SUGGEST_SEC",     3 * 24 * 3600)   #  3 days
TTL_TRENDING_SEC   = _env_int("NHCACHE_TTL_TRENDING_SEC",              1800)   # 30 min
TTL_STALE_GRACE_SEC = _env_int("NHCACHE_TTL_STALE_GRACE_SEC", 7 * 24 * 3600)   # keep stale docs around this long for "serve-stale-if-error"


# ---------------------------------------------------------------------------
# Token-bucket capacities (per minute) — sourced from openapi.json ANON tier.
# See nhentai.net/api/v2/openapi.json — these are documented, not guessed.
# ---------------------------------------------------------------------------
BUCKETS = {
    # bucket_id       : (capacity_per_min, human_label)
    "search"          : (10, "GET /api/v2/search"),
    "galleries"       : (20, "GET /api/v2/galleries/{id}"),
    "galleries_list"  : (15, "GET /api/v2/galleries"),
    "popular"         : ( 8, "GET /api/v2/galleries/popular"),
    "suggestions"     : (60, "GET /api/v2/galleries/{id}/suggestions"),
}


# ---------------------------------------------------------------------------
# Lazy db-handle acquisition. Import-time db.connect() would break under
# pytest and any tool that doesn't have MONGO_URI set. Every public function
# calls _handle() and gracefully returns on failure.
# ---------------------------------------------------------------------------
_conn = None  # cached MongoHandle


def _handle():
    global _conn
    if _conn is not None:
        return _conn
    try:
        import db as _db  # local import — sys.path is set by scraper_bridge
        _conn = _db.connect()
    except Exception as e:  # noqa: BLE001
        log.warning("nhentai_cache: mongo unavailable (%s) — cache disabled", e)
        _conn = None
    return _conn


# ---------------------------------------------------------------------------
# Cache-key helpers. Deterministic, short, human-readable when possible.
# ---------------------------------------------------------------------------
def gallery_key(gid: str | int) -> str:
    return f"gallery:{gid}"


def search_key(query: str, sort: str, page: int) -> str:
    q = (query or "").strip().lower()
    s = (sort or "popular").strip().lower()
    p = int(page or 1)
    # Long queries get hashed to keep the _id compact; short queries stay legible.
    if len(q) <= 40 and all(c.isalnum() or c in " -_" for c in q):
        return f"search:{q}|{s}|{p}"
    h = hashlib.sha1(q.encode("utf-8")).hexdigest()[:16]
    return f"search:{h}|{s}|{p}"


def suggestions_key(gid: str | int) -> str:
    return f"suggest:{gid}"


def trending_key(kind: str = "popular") -> str:
    return f"trending:{kind}"


def bucket_for_key(key: str) -> str:
    """Map a cache key to its token-bucket id — used to pick the right
    quota bucket before firing the upstream call."""
    if key.startswith("gallery:"):     return "galleries"
    if key.startswith("search:"):      return "search"
    if key.startswith("suggest:"):     return "suggestions"
    if key.startswith("trending:"):    return "popular"
    return "galleries_list"


def ttl_for_key(key: str) -> int:
    if key.startswith("gallery:"):  return TTL_GALLERY_SEC
    if key.startswith("search:"):   return TTL_SEARCH_SEC
    if key.startswith("suggest:"):  return TTL_SUGGEST_SEC
    if key.startswith("trending:"): return TTL_TRENDING_SEC
    return TTL_SEARCH_SEC


# ---------------------------------------------------------------------------
# Cache API
# ---------------------------------------------------------------------------
def get(key: str, allow_stale: bool = False) -> Optional[dict]:
    """Return the cached payload for `key`, or None.

    `allow_stale=True` lets the caller pull a doc that's past its expires_at
    but still within TTL_STALE_GRACE_SEC. This is what powers
    'upstream 429 → serve stale-if-error' semantics.
    """
    conn = _handle()
    if conn is None:
        return None
    try:
        doc = conn.nhentai_cache.find_one({"_id": key})
    except Exception:  # noqa: BLE001
        return None
    if not doc:
        return None
    now = _now_dt()
    exp = doc.get("expires_at")
    if exp and exp > now:
        return doc.get("payload")
    if allow_stale and exp:
        stale_cutoff = now - timedelta(seconds=TTL_STALE_GRACE_SEC)
        if exp > stale_cutoff:
            return doc.get("payload")
    return None


def put(key: str, payload: Any, ttl_sec: Optional[int] = None) -> bool:
    """Write a cache entry. `payload` must be JSON-serialisable."""
    conn = _handle()
    if conn is None:
        return False
    ttl = int(ttl_sec if ttl_sec is not None else ttl_for_key(key))
    # Guard against garbage payloads that would poison the cache.
    try:
        json.dumps(payload, default=str)
    except (TypeError, ValueError):
        log.warning("nhentai_cache.put(%s): payload not JSON-serialisable — skipped", key)
        return False
    doc = {
        "_id":        key,
        "payload":    payload,
        "expires_at": _now_dt() + timedelta(seconds=ttl),
        "cached_at":  _now_dt(),
        "ttl_sec":    ttl,
    }
    try:
        conn.nhentai_cache.replace_one({"_id": key}, doc, upsert=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("nhentai_cache.put(%s) failed: %s", key, e)
        return False


def invalidate(key: str) -> None:
    """Force-delete a cache entry (used by admin /force-rescrape)."""
    conn = _handle()
    if conn is None:
        return
    try:
        conn.nhentai_cache.delete_one({"_id": key})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Token bucket — SHARED across users. Prevents any single search from
# blowing past the openapi.json quota and 429ing everyone.
# ---------------------------------------------------------------------------
def try_consume(bucket_id: str, cost: float = 1.0) -> bool:
    """Consume `cost` tokens from `bucket_id`. Return True on success.

    On failure the CALLER must serve from cache (allow_stale=True) instead
    of firing the upstream call. This is the mechanism that keeps one heavy
    search from starving everyone else.
    """
    conn = _handle()
    if conn is None:
        # No mongo → no bucket enforcement. Fail open so the app still works
        # in dev / test setups. In production Mongo is always available.
        return True
    cap, _label = BUCKETS.get(bucket_id, (10, bucket_id))
    rate_per_sec = cap / 60.0
    now = time.time()
    try:
        doc = conn.nhentai_ratelimit.find_one({"_id": bucket_id})
        if doc is None:
            doc = {
                "_id": bucket_id, "tokens": float(cap),
                "capacity": cap, "rate_per_sec": rate_per_sec,
                "updated_at": now,
            }
        # Refill: elapsed seconds × rate, clamped to capacity.
        elapsed = max(0.0, now - float(doc.get("updated_at") or now))
        tokens = min(float(cap), float(doc.get("tokens") or cap) + elapsed * rate_per_sec)
        if tokens < cost:
            # Persist the refill even on refusal so the next caller sees the
            # correct remaining count.
            conn.nhentai_ratelimit.update_one(
                {"_id": bucket_id},
                {"$set": {"tokens": tokens, "updated_at": now,
                          "capacity": cap, "rate_per_sec": rate_per_sec}},
                upsert=True,
            )
            return False
        tokens -= cost
        conn.nhentai_ratelimit.update_one(
            {"_id": bucket_id},
            {"$set": {"tokens": tokens, "updated_at": now,
                      "capacity": cap, "rate_per_sec": rate_per_sec}},
            upsert=True,
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("token bucket %s failed (%s) — failing open", bucket_id, e)
        return True


def bucket_state(bucket_id: str) -> dict:
    """Diagnostic: current tokens / capacity for a bucket. Cheap read."""
    conn = _handle()
    cap, label = BUCKETS.get(bucket_id, (0, bucket_id))
    if conn is None:
        return {"bucket": bucket_id, "label": label, "capacity": cap,
                "tokens": cap, "available": True, "backend": "no-mongo"}
    try:
        doc = conn.nhentai_ratelimit.find_one({"_id": bucket_id})
        tokens = float(doc.get("tokens") if doc else cap)
        elapsed = max(0.0, time.time() - float(doc.get("updated_at") if doc else time.time()))
        tokens = min(float(cap), tokens + elapsed * (cap / 60.0))
        return {"bucket": bucket_id, "label": label, "capacity": cap,
                "tokens": tokens, "available": tokens >= 1.0, "backend": "mongo"}
    except Exception as e:  # noqa: BLE001
        return {"bucket": bucket_id, "label": label, "error": str(e)}


def all_buckets_state() -> list[dict]:
    """Diagnostic: state of every configured bucket."""
    return [bucket_state(bid) for bid in BUCKETS]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
def _now_dt() -> datetime:
    # UTC datetime because Mongo TTL indexes require a BSON Date, not epoch.
    return datetime.now(tz=timezone.utc)
