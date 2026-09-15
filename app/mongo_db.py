"""MongoDB / Atlas layer (v3.0) — the async twin of db.py.

Design mirrors the Turso layer:
  * Collections + every index auto-created on first boot (no manual Atlas work).
  * Single lazy AsyncMongoClient, reconnect-on-failure with exponential backoff.
  * Numeric ids preserved via the `counters` collection (posts.id, post_number)
    so favorites and save:<id> callbacks stay byte-identical to the Turso era.
  * NULL semantics preserved by OMITTING fields (Mongo's `null` matches missing
    in equality queries, and unique indexes treat missing == null like SQLite).

Collection ↔ table mapping (1:1 with db.py schema):
  posts, channels, settings, admins, favorites, user_directory,
  backup_progress, backup_history  +  counters (new, Mongo-only)

Field-name changes vs the SQL schema (NULL → "field absent"):
  posts.id          → _id  (int, from counters['post_id'])
  every nullable column is simply absent when unset.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from pymongo import ASCENDING, IndexModel
from pymongo.errors import (
    AutoReconnect, ConnectionFailure, NetworkTimeout,
    NotPrimaryError, ServerSelectionTimeoutError,
)
from pymongo import AsyncMongoClient

from .config import settings

log = logging.getLogger("mongo_db")

_client: Optional[AsyncMongoClient] = None
_db = None

RETRYABLE = (AutoReconnect, ConnectionFailure, NetworkTimeout,
             NotPrimaryError, ServerSelectionTimeoutError)


def _get_db():
    global _client, _db
    if _db is not None:
        return _db
    uri = (settings.mongodb_uri or "").strip()
    if not uri:
        raise RuntimeError("MONGODB_URI is not set")
    _client = AsyncMongoClient(
        uri,
        serverSelectionTimeoutMS=10000,
        maxPoolSize=20,
        retryWrites=True,
    )
    _db = _client[settings.mongodb_db_name or "telegram_file_bot"]
    return _db


async def reset_conn() -> None:
    """Force reconnect on next call (used by the retry wrapper).
    AsyncMongoClient.close() is awaitable in PyMongo 4.17."""
    global _client, _db
    try:
        if _client is not None:
            await _client.close()
    except Exception:
        pass
    _client = None
    _db = None


async def with_retry(coro_factory, *, attempts: int = 4):
    """Run an async Mongo op, reconnecting with backoff on transient errors."""
    last: Optional[BaseException] = None
    for i in range(1, attempts + 1):
        try:
            return await coro_factory(_get_db())
        except RETRYABLE as e:
            last = e
            log.warning("mongo: transient error, reconnecting (%s/%s): %s",
                        i, attempts, e)
            await reset_conn()
            await asyncio.sleep(min(2 * i, 8))
    if last:
        raise last
    raise RuntimeError("mongo: unknown failure")


# =============================================================================
# Counters — atomic numeric id allocation (replaces AUTOINCREMENT / MAX(#N)).
# find_one_and_update($inc, upsert) is atomic on a single document, so two
# concurrent publishes can never share a #N — same guarantee as the SQL
# UPDATE-with-subquery in repo.mark_published.
# =============================================================================
async def next_counter(name: str) -> int:
    """Atomically increment counters[name] and return the NEW value."""
    async def _op(db):
        doc = await db.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=True,          # ReturnDocument.AFTER
        )
        return int(doc["seq"])
    return await with_retry(_op)


async def get_counter(name: str) -> int:
    async def _op(db):
        d = await db.counters.find_one({"_id": name})
        return int(d["seq"]) if d else 0
    return await with_retry(_op)


async def set_counter(name: str, value: int) -> None:
    """Set counters[name] to an exact value (used by /jumpto + /queue_reset
    so the next assigned #N matches Turso's recompute-from-MAX behaviour)."""
    async def _op(db):
        await db.counters.update_one(
            {"_id": name}, {"$set": {"seq": int(value)}}, upsert=True)
    await with_retry(_op)


async def set_counter_floor(name: str, floor: int) -> None:
    """Raise counters[name] to at least `floor` (migration seeding).
    Never lowers an existing higher value. Uses atomic $max to avoid the
    filter+upsert duplicate-key race the naive version had."""
    async def _op(db):
        await db.counters.update_one(
            {"_id": name},
            {"$max": {"seq": int(floor)}},
            upsert=True,
        )
    await with_retry(_op)


# =============================================================================
# Schema — collections are auto-created on first write; we only create indexes.
# =============================================================================
INDEXES = {
    "posts": [
        IndexModel([("code", ASCENDING)], unique=True, name="uq_code"),
        IndexModel([("source_chat_id", ASCENDING), ("source_message_id", ASCENDING)],
                   unique=True, name="uq_source"),
        IndexModel([("kind", ASCENDING), ("published_at", ASCENDING)],
                   name="idx_kind_pub"),
        IndexModel([("source_chat_id", ASCENDING), ("parent_source_message_id", ASCENDING)],
                   name="idx_parent"),
        IndexModel([("post_number", ASCENDING)], name="idx_number"),
    ],
    "channels": [
        IndexModel([("role", ASCENDING)], name="idx_role"),
    ],
    "settings": [],
    "verified_users": [{"keys": [("until", 1)], "expireAfterSeconds": 0, "name": "ttl_until"},],
    "admins": [],
    "favorites": [
        IndexModel([("user_id", ASCENDING)], name="idx_fav_user"),
    ],
    "user_directory": [],
    "backup_progress": [],
    "backup_history": [
        IndexModel([("backup_chat_id", ASCENDING), ("reset_at", ASCENDING)],
                   name="idx_bh_chat"),
    ],
    "counters": [],
}


async def init_schema() -> None:
    """Create every collection + index. Idempotent. Also pings the server."""
    async def _op(db):
        await db.command("ping")
        existing = set(await db.list_collection_names())
        for name, models in INDEXES.items():
            if name not in existing:
                await db.create_collection(name)
            if models:
                await db[name].create_indexes(models)
        return True
    await with_retry(_op)
    log.info("Mongo schema initialised (db=%s, %d collections)",
             settings.mongodb_db_name, len(INDEXES))
