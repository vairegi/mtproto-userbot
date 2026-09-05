"""
mongo2_client.py — v12.60/v1.30: shared Mongo-2 (cache cluster) client.

Dual-write + Mongo-first read for the nhentai_cache mirror. All three bots
vendor this file (bot-specific path noted in each import site). Behaviour:

  WRITE (dual_write): Turso first (unchanged), then Mongo-2 upsert.
      Logs ONE visible line per write so Render shows both backends:
          ✍️  DUAL-WRITE key=gallery:123 turso=OK mongo2=OK (58ms)
      Mongo-2 failure NEVER blocks — the system degrades to Turso-only.

  READ (mongo2_first_get): Mongo-2 find_one -> on miss, caller falls back
      to Turso and calls backfill() so the row self-heals into Mongo-2.

  TTL: Mongo TTL index on expires_at (expireAfterSeconds=0) mirrors Turso's
      TTL semantics; expires_at==0 sentinel rows are excluded (never expire).

Env (same names on ALL 3 services):
  MONGO2_URI / MONGO2_BACKUP_URI / 2NDMONGO_BACKUP_TURSO — cluster URI
  MONGO2_DB      — default "turso_backup" (reuses the mirror DB)
  MONGO2_READS   — "1" Mongo-first (default), "0" Turso-only (rollback)
  MONGO2_WRITES  — "1" dual-write (default), "0" Turso-only writes
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("mongo2")

_COLL = "turso_nhentai_cache"   # matches the v1.28 backup mirror
_client = None
_ready = False


def mongo2_uri() -> str:
    for name in ("MONGO2_URI", "MONGO2_BACKUP_URI", "2NDMONGO_BACKUP_TURSO",
                 "2ndmongo_backup_turso"):
        v = (os.getenv(name) or "").strip()
        if v:
            return v
    return ""


def mongo2_db() -> str:
    return (os.getenv("MONGO2_DB") or "turso_backup").strip() or "turso_backup"


def reads_enabled() -> bool:
    return os.getenv("MONGO2_READS", "1").strip().lower() in ("1", "true", "on", "yes")


def writes_enabled() -> bool:
    return os.getenv("MONGO2_WRITES", "1").strip().lower() in ("1", "true", "on", "yes")


def _get_coll():
    """Lazily connect + ensure the TTL index. None when unconfigured."""
    global _client, _ready
    if _client is None:
        uri = mongo2_uri()
        if not uri:
            return None
        try:
            from pymongo import MongoClient
            _client = MongoClient(uri, serverSelectionTimeoutMS=15000)
        except Exception as e:  # noqa: BLE001
            log.warning("mongo2: connect failed: %s", e)
            return None
    coll = _client[mongo2_db()][_COLL]
    if not _ready:
        try:
            # TTL mirror: expires_at (epoch seconds) -> Mongo removes the doc
            # when its time passes. expires_at==0 (never-expire sentinel) is
            # excluded from the index so those rows persist.
            coll.create_index(
                "expires_at",
                expireAfterSeconds=0,
                partialFilterExpression={"expires_at": {"$gt": 0}},
                name="ttl_expires_at",
                background=True,
            )
            _ready = True
        except Exception as e:  # noqa: BLE001
            log.warning("mongo2: TTL index ensure failed (non-fatal): %s", e)
            _ready = True   # don't retry every call
    return coll


def put(key: str, payload_json: str, expires_at: int, ttl_sec: int,
        cached_at: Optional[int] = None) -> bool:
    """Upsert one row into the mirror. Returns True on success."""
    if not writes_enabled():
        return False
    coll = _get_coll()
    if coll is None:
        return False
    now = int(cached_at or time.time())
    doc = {"_id": key, "key": key, "payload": payload_json,
           "cached_at": now, "expires_at": int(expires_at or 0),
           "ttl_sec": int(ttl_sec or 0)}
    try:
        coll.replace_one({"_id": key}, doc, upsert=True)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("mongo2: put(%s) failed: %s", key, e)
        return False


def get(key: str) -> Optional[dict]:
    """Mongo-first read. Returns {"payload": <json str>, "expires_at": int}
    or None on miss / disabled / unavailable."""
    if not reads_enabled():
        return None
    coll = _get_coll()
    if coll is None:
        return None
    try:
        doc = coll.find_one({"_id": key}, {"payload": 1, "expires_at": 1})
        if not doc:
            log.info("📖 MONGO2 MISS key=%s (falling back to Turso)", key)
            return None
        log.info("📖 MONGO2 HIT key=%s", key)
        return {"payload": doc.get("payload"),
                "expires_at": int(doc.get("expires_at") or 0)}
    except Exception as e:  # noqa: BLE001
        log.warning("mongo2: get(%s) failed: %s — Turso fallback", key, e)
        return None


def log_dual_write(key: str, turso_ok: bool, mongo2_ok: bool, ms: int) -> None:
    """The visible Render-log line the user asked for — one per write."""
    log.info("✍️  DUAL-WRITE key=%s turso=%s mongo2=%s (%dms)",
             key, "OK" if turso_ok else "FAIL",
             "OK" if mongo2_ok else "FAIL", ms)
