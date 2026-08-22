"""
bot0_hints.py — v12.34b cross-bot cache-warming hints.

When miniminiapp search/detail requests hit a cold Turso row, the first
user pays the upstream fetch. To eliminate that tax the Mini App fires a
"user wants this gallery ID" hint into the same MongoDB collection BOT 1
already uses (`scraper1_state`). BOT 1's `details_sweeper` drains the hint
queue on its very next tick, fetches the gallery, and warms Turso so the
next user (and the user themselves on a refresh) hits the cache.

Storage: single Mongo doc `_id = "user_gallery_hints"`, `value` is a list
of gallery ID strings. Trimmed to 200 entries by gravity (oldest dropped
first). Both ends are best-effort: a Mongo outage MUST NOT break a Mini
App request, NOR a BOT 1 sweep tick. Cross-region safe because Mongo is
the only shared backend; the existing BOT 1 region suffix
(`BOT1_REGION=ap-singapore`) does not affect this collection, hints are
useful on every region.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

log = logging.getLogger("bot0.hints")

_HINTS_DOC_ID = "user_gallery_hints"
_HINTS_CAP = 200                # max entries retained
_HINTS_TRIM_TO = 150            # when over cap, drop oldest to this size

_client_lock = threading.Lock()
_client: Optional["object"] = None  # pymongo.MongoClient

_DB_NAME_DEFAULT = "relaybot"


def _db_name() -> str:
    return (os.environ.get("MONGO_DB_NAME") or _DB_NAME_DEFAULT).strip() or _DB_NAME_DEFAULT


def _mongo_uri() -> str:
    return (os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI") or "").strip()


def _get_client():
    """Lazily create one MongoClient per process. Re-create on fork (BOT 0
    never forks but defence in depth)."""
    global _client
    uri = _mongo_uri()
    if not uri:
        return None
    with _client_lock:
        if _client is not None:
            return _client
        try:
            from pymongo import MongoClient  # local import — never blocks import
            _client = MongoClient(
                uri,
                serverSelectionTimeoutMS=4000,
                connectTimeoutMS=4000,
                socketTimeoutMS=4000,
                retryWrites=True,
                maxPoolSize=4,
            )
            _client.admin.command("ping")
            log.info("bot0_hints: mongo connected (db=%s)", _db_name())
        except Exception as e:  # noqa: BLE001
            log.warning("bot0_hints: mongo connect failed (%s) — hints disabled", e)
            _client = None
        return _client


def _coll():
    c = _get_client()
    if c is None:
        return None
    return c[_db_name()]["scraper1_state"]


def hint_push_gid(gid) -> bool:
    """Best-effort enqueue of one numeric gallery ID. Safe to call from any
    hot path: failures are logged at debug and swallowed."""
    if gid is None or gid == "":
        return False
    gid = str(gid).strip()
    if not gid:
        return False
    coll = _coll()
    if coll is None:
        return False
    try:
        # Step 1: append. Insert-or-create the doc if absent.
        coll.update_one(
            {"_id": _HINTS_DOC_ID},
            {"$push": {"value": gid}, "$set": {"updated_at": time.time()}},
            upsert=True,
        )
        # Step 2: gravity trim — keep only the newest _HINTS_CAP entries.
        # $slice with negative index keeps the LAST N elements.
        coll.update_one(
            {"_id": _HINTS_DOC_ID},
            [
                {"$set": {
                    "value": {
                        "$slice": [
                            {"$ifNull": ["$value", []]},
                            -_HINTS_CAP,
                        ]
                    }
                }}
            ],
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.debug("bot0_hints: hint_push failed for %s: %s", gid, e)
        return False


def hint_queue_size() -> int:
    coll = _coll()
    if coll is None:
        return 0
    try:
        doc = coll.find_one({"_id": _HINTS_DOC_ID}, {"value": 1})
        if not doc:
            return 0
        v = doc.get("value") or []
        return len(v) if isinstance(v, list) else 0
    except Exception:  # noqa: BLE001
        return 0
