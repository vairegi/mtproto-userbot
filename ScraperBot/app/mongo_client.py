"""
mongo_client.py — thin wrapper around the SAME MongoDB cluster BOT 0 uses.

BOT 1 only reads/writes two collections:
  * nhentai_cache          — same schema BOT 0's nhentai_cache.py uses
  * scraper1_state         — BOT 1-only state (sweep positions, pause flag)

We deliberately do NOT touch queue / users / admins / galleries / progress_*
— those belong to BOT 0.
"""
from __future__ import annotations

import threading
import time as _time

import logging
import time
from typing import Any, Dict, Optional

from pymongo import ASCENDING, MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

from .config import settings

log = logging.getLogger("scraperbot.mongo")

_client: Optional[MongoClient] = None
_lock = threading.Lock()


def _connect() -> Optional[MongoClient]:
    global _client
    with _lock:
        if _client is not None:
            return _client
        if not settings.mongo_uri:
            log.error("MONGO_URI unset — Mongo disabled")
            return None
        try:
            _client = MongoClient(
                settings.mongo_uri,
                serverSelectionTimeoutMS=8000,
                connectTimeoutMS=8000,
                maxPoolSize=8,
                retryWrites=True,
            )
            # Force a round-trip so we fail fast on bad URIs.
            _client.admin.command("ping")
            log.info("Mongo connected (db=%s)", settings.mongo_db_name)
        except Exception as e:  # noqa: BLE001
            log.error("Mongo connect failed: %s", e)
            _client = None
        return _client


def db():
    c = _connect()
    if c is None:
        return None
    return c[settings.mongo_db_name]


# ---------------------------------------------------------------------------
# scraper1_state — BOT 1's private key/value store (sweep cursors, pause flag)
# ---------------------------------------------------------------------------
STATE_COLL = "scraper1_state"


# v1.22.9: in-process read-through cache for scraper1_state. The sweepers,
# dashboard and activity recorder call state_get/is_paused/state_set on the
# uvicorn EVENT LOOP every few seconds; a single synchronous pymongo call on
# a cold Atlas M0 socket blocks up to serverSelectionTimeoutMS (8s) — longer
# than Render's 5s /healthz window — and got the instance SIGTERMed on a
# loop ("HTTP health check failed (timed out after 5 seconds)" while RAM sat
# at 84MB). Reads now serve from an 8s TTL cache refreshed in a daemon
# thread; writes update the cache instantly and persist in the background.
_STATE_CACHE: dict = {}
_STATE_CACHE_TTL = 8.0
_STATE_LOCK = threading.Lock()


def _state_fetch_sync(key: str, default: Any = None) -> Any:
    d = db()
    if d is None:
        return default
    try:
        doc = d[STATE_COLL].find_one({"_id": key})
        if not doc:
            return default
        return doc.get("value", default)
    except PyMongoError as e:
        log.warning("state_get(%s) failed: %s", key, e)
        return default


def state_get(key: str, default: Any = None) -> Any:
    now = _time.monotonic()
    with _STATE_LOCK:
        hit = _STATE_CACHE.get(key)
    if hit is not None and (now - hit[1]) < _STATE_CACHE_TTL:
        return hit[0]
    if hit is None:
        # First-ever read of this key: one synchronous fetch, then cached.
        v = _state_fetch_sync(key, default)
        with _STATE_LOCK:
            _STATE_CACHE[key] = (v, now)
        return v
    # Stale hit: serve it NOW, refresh in background — never block the loop.
    def _refresh() -> None:
        v = _state_fetch_sync(key, default)
        with _STATE_LOCK:
            _STATE_CACHE[key] = (v, _time.monotonic())
    threading.Thread(target=_refresh, daemon=True).start()
    return hit[0]


def _state_set_sync(key: str, value: Any) -> bool:
    d = db()
    if d is None:
        return False
    try:
        d[STATE_COLL].update_one(
            {"_id": key},
            {"$set": {"value": value, "updated_at": time.time()}},
            upsert=True,
        )
        return True
    except PyMongoError as e:
        log.warning("state_set(%s) failed: %s", key, e)
        return False



def state_set(key: str, value: Any) -> bool:
    """v1.22.9: update the in-process cache immediately (all readers see it
    this instant), persist to Mongo in a daemon thread. Never blocks the
    event loop on a cold Atlas socket. Returns True optimistically — stats
    and cursors tolerate a rare dropped write; /pause correctness is kept
    because the cache update is synchronous."""
    with _STATE_LOCK:
        _STATE_CACHE[key] = (value, _time.monotonic())
    def _persist() -> None:
        try:
            _state_set_sync(key, value)
        except Exception as e:  # noqa: BLE001
            log.warning("state_set(%s) background persist failed: %s", key, e)
    threading.Thread(target=_persist, daemon=True).start()
    return True



def is_paused() -> bool:
    return bool(state_get("paused", False))


def set_paused(flag: bool) -> None:
    state_set("paused", bool(flag))


# ---------------------------------------------------------------------------
# v12.34b: cross-bot user-hint queue (BOT 0 writes; BOT 1 reads).
#
# Storage: single doc in `scraper1_state` keyed `_id = "user_gallery_hints"`,
# `value` = FIFO list of numeric gallery IDs. Trimmed to 200 by gravity
# inside bot0_hints.hint_push_gid prior to the push, so the consumer can
# assume a bounded list. Pop returns the FIRST `n` entries and rewrites the
# remainder back into the doc atomically (one round trip).
# ---------------------------------------------------------------------------
_HINTS_DOC_ID = "user_gallery_hints"


def hint_pop_gids(n: int = 4) -> list:
    """Atomically pop up to `n` gallery IDs from the BOT-0 hint queue.

    Returns a list (possibly empty). Failures are logged and return []
    so a Mongo blip never stalls the sweeper. Cross-region safe — the
    hint queue is shared across all BOT 1 instances."""
    n = max(0, int(n or 0))
    if n == 0:
        return []
    d = db()
    if d is None:
        return []
    try:
        # $slice with positive start, very large end takes the FIRST n
        # elements out of the array. The doc retains the tail automatically
        # because array element removal with $slice resumes the rest.
        doc = d[STATE_COLL].find_one_and_update(
            {"_id": _HINTS_DOC_ID},
            [
                {"$set": {
                    "value": {
                        "$slice": [
                            {"$ifNull": ["$value", []]},
                            n,
                            999999,
                        ]
                    },
                    "updated_at": time.time(),
                }},
            ],
            return_document=ReturnDocument.AFTER,
        )
        if not doc:
            return []
        v = doc.get("value") or []
        if not isinstance(v, list):
            return []
        # Return at most n (the doc now contains the remainder).
        return [str(x).strip() for x in v[:n] if x is not None]
    except PyMongoError as e:
        log.warning("hint_pop_gids failed: %s", e)
        return []


def hint_queue_size() -> int:
    d = db()
    if d is None:
        return 0
    try:
        doc = d[STATE_COLL].find_one({"_id": _HINTS_DOC_ID}, {"value": 1})
        if not doc:
            return 0
        v = doc.get("value") or []
        return len(v) if isinstance(v, list) else 0
    except PyMongoError:
        return 0


# ---------------------------------------------------------------------------
# Mirror of BOT 0's nhentai_cache Mongo fallback — same collection name.
# ---------------------------------------------------------------------------
CACHE_COLL = "nhentai_cache"


def cache_ensure_indexes() -> None:
    d = db()
    if d is None:
        return
    try:
        d[CACHE_COLL].create_index([("expires_at", ASCENDING)])
        d[STATE_COLL].create_index([("updated_at", ASCENDING)])
    except PyMongoError as e:
        log.warning("cache index create failed: %s", e)


def cache_get_mongo(key: str) -> Optional[Dict[str, Any]]:
    d = db()
    if d is None:
        return None
    try:
        return d[CACHE_COLL].find_one({"_id": key})
    except PyMongoError as e:
        log.warning("cache_get_mongo(%s) failed: %s", key, e)
        return None


def cache_put_mongo(key: str, payload: Any, ttl_sec: int) -> bool:
    d = db()
    if d is None:
        return False
    now = time.time()
    # v1.12: ttl_sec == 0 is the never-expire sentinel. Store expires_at=0
    # verbatim; BOT 0's nhentai_cache._mongo_get (v12.20) recognises the
    # numeric zero as always-fresh. Non-zero ttl behaves as before.
    ttl_i = int(ttl_sec)
    expires_at_val = 0 if ttl_i == 0 else (now + ttl_i)
    try:
        d[CACHE_COLL].update_one(
            {"_id": key},
            {"$set": {
                "payload": payload,
                "updated_at": now,
                "expires_at": expires_at_val,
                "writer": "scraperbot",
            }},
            upsert=True,
        )
        return True
    except PyMongoError as e:
        log.warning("cache_put_mongo(%s) failed: %s", key, e)
        return False


# ---------------------------------------------------------------------------
# Shared token bucket — v1.14: Turso-first, Mongo fallback.
#
# Historically BOT 1 wrote to Mongo collection `nhentai_bucket` while BOT 0
# wrote to Turso table `nhentai_ratelimit`. They never saw each other's
# consumption, so the "shared" quota was a lie — this is why the BOT 1
# dashboard showed 141+ bucket skips while BOT 0 was still fetching pages.
#
# After v1.14, BOTH bots use Turso `nhentai_ratelimit` (token-bucket schema:
# {bucket_id, tokens, capacity, rate_per_sec, updated_at}) with identical
# refill math. Mongo `nhentai_bucket` is kept ONLY as a fail-open fallback
# when Turso is unreachable, so a Turso outage doesn't stall the sweep.
# ---------------------------------------------------------------------------
BUCKET_COLL = "nhentai_bucket"   # kept for the Mongo fallback only


async def _turso_bucket_try_consume(bucket: str, capacity_per_min: int) -> Optional[bool]:
    """Turso-backed atomic consume — byte-identical algorithm to BOT 0's
    `_turso_try_consume` so both bots share one bucket.

    Returns True on success, False when the bucket is exhausted, None when
    Turso is unreachable (caller falls back to Mongo)."""
    try:
        from . import turso_client
    except Exception:  # noqa: BLE001
        return None
    if not turso_client.turso_available():
        return None

    now = time.time()
    rate_per_sec = float(capacity_per_min) / 60.0

    # INSERT OR IGNORE — no-op if the row exists.
    try:
        res = await turso_client.execute(
            "INSERT OR IGNORE INTO nhentai_ratelimit "
            "(bucket_id, tokens, capacity, rate_per_sec, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            [bucket, float(capacity_per_min), float(capacity_per_min),
             rate_per_sec, now],
        )
        if res is None:
            return None
    except Exception as e:  # noqa: BLE001
        log.debug("bucket_try_consume turso insert failed: %s", e)
        return None

    # Atomic refill-and-spend — same SQL BOT 0 uses. The WHERE clause
    # prevents over-spend; if it fails, zero rows are affected and we
    # return False without touching updated_at (so the next caller still
    # gets the full refill they earned).
    try:
        res = await turso_client.execute(
            "UPDATE nhentai_ratelimit SET "
            "  tokens = MIN(CAST(capacity AS REAL), tokens + MAX(0, ? - updated_at) * rate_per_sec) - ?, "
            "  updated_at = ? "
            "WHERE bucket_id = ? "
            "  AND MIN(CAST(capacity AS REAL), tokens + MAX(0, ? - updated_at) * rate_per_sec) >= ?",
            [now, 1.0, now, bucket, now, 1.0],
        )
        if res is None:
            return None
    except Exception as e:  # noqa: BLE001
        log.debug("bucket_try_consume turso update failed: %s", e)
        return None

    # libsql returns affected-row count in rows_affected; guard both.
    affected = res.get("affected_row_count")
    if affected is None:
        affected = res.get("rows_affected")
    if affected is None:
        # Older response shape — probe the row to see if tokens went negative.
        try:
            probe = await turso_client.execute(
                "SELECT tokens FROM nhentai_ratelimit WHERE bucket_id = ?",
                [bucket],
            )
            if probe and probe.get("rows"):
                row = probe["rows"][0]
                # libsql cells are {type, value} dicts
                cell = row[0] if isinstance(row, list) and row else None
                tok = None
                if isinstance(cell, dict):
                    v = cell.get("value")
                    try:
                        tok = float(v)
                    except (TypeError, ValueError):
                        tok = None
                if tok is not None:
                    return tok >= 0
        except Exception:  # noqa: BLE001
            pass
        return None
    return bool(int(affected) > 0)


def _mongo_bucket_try_consume(bucket: str, capacity_per_min: int) -> bool:
    """Mongo fallback — original sliding-window logic from pre-v1.14.
    Used ONLY when Turso is unreachable so a Turso outage doesn't stall
    the sweep entirely."""
    d = db()
    if d is None:
        # Fail-open: no Mongo → let the call through (we still respect
        # the delay knobs). Better than stalling the sweep entirely.
        return True
    now = time.time()
    window_start = now - 60.0
    try:
        # Atomic pop-old + push-new via aggregation pipeline update.
        # If under capacity after the push, we consumed; else rollback push.
        doc = d[BUCKET_COLL].find_one_and_update(
            {"_id": bucket},
            [
                {"$set": {
                    "ts": {
                        "$filter": {
                            "input": {"$ifNull": ["$ts", []]},
                            "as": "t",
                            "cond": {"$gte": ["$$t", window_start]},
                        }
                    }
                }},
            ],
            upsert=True,
            return_document=True,
        )
        current = (doc or {}).get("ts", []) or []
        if len(current) >= int(capacity_per_min):
            return False
        d[BUCKET_COLL].update_one(
            {"_id": bucket},
            {"$push": {"ts": now}},
        )
        return True
    except PyMongoError as e:
        log.warning("bucket_try_consume(%s) failed (fail-open): %s", bucket, e)
        return True


async def bucket_try_consume(bucket: str, capacity_per_min: int) -> bool:
    """Turso-first, Mongo-fallback token bucket.

    v1.14: Turso is the primary store so BOT 0 + BOT 1 share one quota.
    Mongo `nhentai_bucket` is kept only as a fail-open fallback for when
    Turso is unreachable (network blip, Turso maintenance, etc.).

    v1.19: the `bucket` id passed in may ALREADY carry a region suffix
    (applied in cache.try_consume from settings.region_suffix). This
    function is region-agnostic — it passes whatever id it receives
    straight through to both the Turso and Mongo paths unchanged, so a
    region-suffixed id (e.g. "search_ap-singapore") transparently uses
    its own row via the existing INSERT OR IGNORE bootstrap (no schema
    migration)."""
    turso_result = await _turso_bucket_try_consume(bucket, capacity_per_min)
    if turso_result is not None:
        return turso_result
    return _mongo_bucket_try_consume(bucket, capacity_per_min)
