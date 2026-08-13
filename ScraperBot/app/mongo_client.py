"""
mongo_client.py — thin wrapper around the SAME MongoDB cluster BOT 0 uses.

BOT 1 only reads/writes two collections:
  * nhentai_cache          — same schema BOT 0's nhentai_cache.py uses
  * scraper1_state         — BOT 1-only state (sweep positions, pause flag)

We deliberately do NOT touch queue / users / admins / galleries / progress_*
— those belong to BOT 0.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, Optional

from pymongo import ASCENDING, MongoClient
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


def state_get(key: str, default: Any = None) -> Any:
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


def state_set(key: str, value: Any) -> bool:
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


def is_paused() -> bool:
    return bool(state_get("paused", False))


def set_paused(flag: bool) -> None:
    state_set("paused", bool(flag))


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
    try:
        d[CACHE_COLL].update_one(
            {"_id": key},
            {"$set": {
                "payload": payload,
                "updated_at": now,
                "expires_at": now + int(ttl_sec),
                "writer": "scraperbot",
            }},
            upsert=True,
        )
        return True
    except PyMongoError as e:
        log.warning("cache_put_mongo(%s) failed: %s", key, e)
        return False


# ---------------------------------------------------------------------------
# Shared token bucket — same collection name BOT 0 uses so BOT 0 + BOT 1
# consume ONE quota per bucket.
# ---------------------------------------------------------------------------
BUCKET_COLL = "nhentai_bucket"


def bucket_try_consume(bucket: str, capacity_per_min: int) -> bool:
    """Sliding-window bucket. Returns True if a token was consumed.

    Same shape BOT 0's nhentai_cache.try_consume uses (list of timestamps
    per bucket), so the two processes coordinate through Mongo alone.
    """
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
