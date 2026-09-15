"""Data-access layer — v3.0 dual backend (Turso / MongoDB Atlas).

Every public function is ASYNC. `DB_BACKEND` env var selects the backend:
  * 'turso' (default) — identical SQL behaviour as v2.9; deploying with no
    env change is a complete no-op.
  * 'mongo' — MongoDB Atlas via the async PyMongo driver (app/mongo_db.py).

Mongo mapping notes:
  * posts.id        → _id (int from counters['post_id']) — numeric ids kept so
                      favorites + save:<id> callbacks are unchanged.
  * post_number     → counters['post_number'], atomic find_one_and_update($inc)
                      — same "no race, no double-scan" guarantee as the SQL
                      UPDATE-with-subquery. /jumpto and /queue_reset reset the
                      counter so next #N matches Turso's recompute-from-MAX.
  * NULL columns    → field simply absent from the document.
  * Composite PKs   → favorites._id = "user:post", backup_progress._id =
                      "backup:db:msg" — same dedupe power as INSERT OR IGNORE.

The 60s TTL in-memory caches are backend-agnostic and unchanged.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, List, Optional

from ..config import settings
from ..db import execute, executemany, insert, query_all, query_one, query_scalar
from ..utils import now_iso, random_code


def _mongo() -> bool:
    return settings.db_backend == "mongo"


# ============================================================================
# TTL cache — 60s. Manual invalidation on writes. (unchanged from v2.9)
# ============================================================================
_CACHE_TTL = 60.0
_cache: dict[str, tuple[float, Any]] = {}


def _cache_get(key: str):
    hit = _cache.get(key)
    if not hit:
        return None
    ts, value = hit
    if time.time() - ts > _CACHE_TTL:
        _cache.pop(key, None)
        return None
    return value


def _cache_set(key: str, value: Any) -> None:
    _cache[key] = (time.time(), value)


def _cache_invalidate(*prefixes: str) -> None:
    if not prefixes:
        _cache.clear()
        return
    for k in list(_cache.keys()):
        if any(k.startswith(p) for p in prefixes):
            _cache.pop(k, None)


def cache_stats() -> dict:
    return {"entries": len(_cache), "ttl_seconds": _CACHE_TTL}


def cache_flush() -> None:
    _cache_invalidate()


# ============================================================================
# Mongo helpers
# ============================================================================
def _row(doc: Optional[dict], id_field: Optional[str] = None,
        fields: Optional[List[str]] = None) -> Optional[dict]:
    """Convert a Mongo doc to a SQL-row-shaped dict (id injected, _id dropped).

    `fields` = the column list the equivalent SQL query returns; any column
    absent from the Mongo doc (NULL → field omitted at write time) is filled
    with None so the row dict has the EXACT same shape as a Turso row.
    """
    if doc is None:
        return None
    d = {k: v for k, v in doc.items() if k != "_id"}
    if id_field:
        d[id_field] = doc["_id"]
    if fields:
        for f in fields:
            if f not in d:
                d[f] = None
    return d


def _rows(docs, id_field: Optional[str] = None,
          fields: Optional[List[str]] = None) -> List[dict]:
    return [_row(d, id_field, fields) for d in docs]


def _drop_nones(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# Full column list of the SQL `posts` table minus `id` — passed to _row() so
# Mongo post rows have the exact same dict shape as SQL rows (missing == NULL).
_POST_ROW_FIELDS = [
    "code", "kind", "media_kind", "source_chat_id", "source_message_id",
    "parent_source_message_id", "caption", "file_id", "file_name",
    "mime_type", "post_number", "published_at", "main_chat_id",
    "main_message_id", "created_at",
]


async def _m_next_post_id() -> int:
    from .. import mongo_db
    return await mongo_db.next_counter("post_id")


async def _m_reserve_post_ids(n: int) -> int:
    """Reserve a block of n post ids. Returns the FIRST id of the block."""
    from .. import mongo_db
    if n <= 0:
        return 0

    async def _op(db):
        doc = await db.counters.find_one_and_update(
            {"_id": "post_id"}, {"$inc": {"seq": n}},
            upsert=True, return_document=True)
        return int(doc["seq"]) - n + 1
    return await mongo_db.with_retry(_op)


async def _m_next_post_number() -> int:
    from .. import mongo_db
    return await mongo_db.next_counter("post_number")


async def _m_reserve_post_numbers(n: int) -> int:
    """Reserve n post numbers. Returns the BASE (first assigned = base+1)."""
    from .. import mongo_db
    if n <= 0:
        return 0

    async def _op(db):
        doc = await db.counters.find_one_and_update(
            {"_id": "post_number"}, {"$inc": {"seq": n}},
            upsert=True, return_document=True)
        return int(doc["seq"]) - n
    return await mongo_db.with_retry(_op)


# ============================================================================
# Settings — cached
# ============================================================================
async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    ck = f"setting:{key}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached if cached != "__NONE__" else default
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.settings.find_one({"_id": key})
        doc = await mongo_db.with_retry(_op)
        val = doc.get("value") if doc else None
    else:
        row = query_one("SELECT value FROM settings WHERE key = ?", (key,))
        val = row["value"] if row else None
    _cache_set(ck, val if val is not None else "__NONE__")
    return val if val is not None else default


async def set_setting(key: str, value: Optional[str]) -> None:
    if _mongo():
        from .. import mongo_db
        if value is None:
            async def _op(db):
                await db.settings.delete_one({"_id": key})
        else:
            async def _op(db):
                await db.settings.update_one(
                    {"_id": key},
                    {"$set": {"key": key, "value": value}},
                    upsert=True)
        await mongo_db.with_retry(_op)
    else:
        if value is None:
            execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    _cache_invalidate(f"setting:{key}")


async def get_setting_bool(key: str, default: bool = False) -> bool:
    v = await get_setting(key)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


async def get_setting_int(key: str, default: int = 0) -> int:
    v = await get_setting(key)
    if v is None:
        return default
    try:
        return int(v)
    except Exception:
        return default


async def get_setting_json(key: str, default: Any = None) -> Any:
    v = await get_setting(key)
    if v is None:
        return default
    try:
        return json.loads(v)
    except Exception:
        return default


async def set_setting_json(key: str, value: Any) -> None:
    await set_setting(key, json.dumps(value, ensure_ascii=False))


# ============================================================================
# Cursor (per DB channel, stored in settings) — cached
# ============================================================================
async def get_cursor(db_chat_id: int) -> int:
    return await get_setting_int(f"cursor:{db_chat_id}", 0)


async def set_cursor(db_chat_id: int, message_id: int) -> None:
    await set_setting(f"cursor:{db_chat_id}", str(int(message_id)))


# ============================================================================
# Channels — cached
# ============================================================================
async def add_channel(chat_id: int, role: str, title: Optional[str] = None) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            # ON CONFLICT: role always replaced; title only if a new one given
            # (mirrors COALESCE(excluded.title, channels.title)).
            await db.channels.update_one(
                {"_id": int(chat_id)},
                [{"$set": {
                    "chat_id": int(chat_id),
                    "role": role,
                    "title": {"$ifNull": [title, "$title"]},
                    "added_at": {"$ifNull": ["$added_at", now_iso()]},
                }}],
                upsert=True)
        await mongo_db.with_retry(_op)
    else:
        execute(
            "INSERT INTO channels(chat_id, role, title) VALUES(?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET role = excluded.role, "
            "title = COALESCE(excluded.title, channels.title)",
            (chat_id, role, title),
        )
    _cache_invalidate("channels:")


async def remove_channel(chat_id: int) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.channels.delete_one({"_id": int(chat_id)})
        await mongo_db.with_retry(_op)
    else:
        execute("DELETE FROM channels WHERE chat_id = ?", (chat_id,))
    _cache_invalidate("channels:")


async def get_channel(chat_id: int) -> Optional[dict]:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.channels.find_one({"_id": int(chat_id)})
        return _row(await mongo_db.with_retry(_op), "chat_id",
                    ["role", "title"])
    return query_one("SELECT chat_id, role, title FROM channels WHERE chat_id = ?",
                     (chat_id,))


async def _channels_by_role(role: str) -> List[dict]:
    ck = f"channels:{role}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.channels.find({"role": role}).sort("_id", 1)
            return await cur.to_list(length=None)
        rows = _rows(await mongo_db.with_retry(_op), "chat_id", ["role", "title"])
    else:
        rows = query_all(
            "SELECT chat_id, role, title FROM channels WHERE role = ? ORDER BY chat_id",
            (role,))
    _cache_set(ck, rows)
    return rows


async def get_database_channels() -> List[dict]:
    return await _channels_by_role("database")


async def get_main_channels() -> List[dict]:
    return await _channels_by_role("main")


async def get_log_channel() -> Optional[dict]:
    rows = await _channels_by_role("log")
    return rows[0] if rows else None


async def get_backup_channels() -> List[dict]:
    return await _channels_by_role("backup")


async def database_chat_ids() -> set:
    """Fast in-memory set for the webhook classifier. Cached."""
    ck = "channels:db_ids_set"
    cached = _cache_get(ck)
    if cached is not None:
        return cached
    ids = {int(c["chat_id"]) for c in await get_database_channels()}
    _cache_set(ck, ids)
    return ids


async def list_all_channels() -> List[dict]:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.channels.find().sort([("role", 1), ("_id", 1)])
            return await cur.to_list(length=None)
        return _rows(await mongo_db.with_retry(_op), "chat_id", ["role", "title"])
    return query_all("SELECT chat_id, role, title FROM channels ORDER BY role, chat_id")


async def update_channel_title(chat_id: int, title: str) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.channels.update_one({"_id": int(chat_id)},
                                         {"$set": {"title": title}})
        await mongo_db.with_retry(_op)
    else:
        execute("UPDATE channels SET title = ? WHERE chat_id = ?", (title, chat_id))
    _cache_invalidate("channels:")


# ============================================================================
# Admins
# ============================================================================
async def is_admin(user_id: int) -> bool:
    ck = f"admin:{user_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return bool(cached)
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.admins.find_one({"_id": int(user_id)}, {"_id": 1})
        val = (await mongo_db.with_retry(_op)) is not None
    else:
        row = query_one("SELECT user_id FROM admins WHERE user_id = ?", (user_id,))
        val = row is not None
    _cache_set(ck, val)
    return val


async def is_super_admin(user_id: int) -> bool:
    ck = f"super:{user_id}"
    cached = _cache_get(ck)
    if cached is not None:
        return bool(cached)
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.admins.find_one({"_id": int(user_id)})
        row = await mongo_db.with_retry(_op)
        val = bool(row and int(row.get("is_super") or 0) == 1)
    else:
        row = query_one("SELECT is_super FROM admins WHERE user_id = ?", (user_id,))
        val = bool(row and int(row.get("is_super") or 0) == 1)
    _cache_set(ck, val)
    return val


async def add_admin(user_id: int, is_super: bool = False) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.admins.update_one(
                {"_id": int(user_id)},
                {"$set": {"user_id": int(user_id),
                          "is_super": 1 if is_super else 0},
                 "$setOnInsert": {"added_at": now_iso()}},
                upsert=True)
        await mongo_db.with_retry(_op)
    else:
        execute(
            "INSERT INTO admins(user_id, is_super) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_super = excluded.is_super",
            (user_id, 1 if is_super else 0),
        )
    _cache_invalidate(f"admin:{user_id}", f"super:{user_id}")


async def remove_admin(user_id: int) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.admins.delete_one({"_id": int(user_id)})
        await mongo_db.with_retry(_op)
    else:
        execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    _cache_invalidate(f"admin:{user_id}", f"super:{user_id}")


async def list_admins() -> List[dict]:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.admins.find().sort("_id", 1)
            return await cur.to_list(length=None)
        return _rows(await mongo_db.with_retry(_op), "user_id",
                     ["is_super", "added_at"])
    return query_all("SELECT user_id, is_super, added_at FROM admins ORDER BY user_id")


# ============================================================================
# Posts — the hot table
# ============================================================================
async def post_exists(source_chat_id: int, source_message_id: int) -> bool:
    """INDEX HIT on UNIQUE(source_chat_id, source_message_id)."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.find_one(
                {"source_chat_id": int(source_chat_id),
                 "source_message_id": int(source_message_id)}, {"_id": 1})
        return (await mongo_db.with_retry(_op)) is not None
    return query_one(
        "SELECT 1 FROM posts WHERE source_chat_id = ? AND source_message_id = ? LIMIT 1",
        (source_chat_id, source_message_id),
    ) is not None


async def insert_cover(source_chat_id: int, source_message_id: int, caption: Optional[str],
                 media_kind: str, file_id: Optional[str], file_name: Optional[str],
                 mime_type: Optional[str] = None) -> Optional[int]:
    """Return new post id, or None if duplicate (INSERT OR IGNORE semantics)."""
    if _mongo():
        from .. import mongo_db
        pid = await _m_next_post_id()
        doc = _drop_nones({
            "_id": pid, "code": random_code(8), "kind": "cover",
            "media_kind": media_kind,
            "source_chat_id": int(source_chat_id),
            "source_message_id": int(source_message_id),
            "caption": caption, "file_id": file_id,
            "file_name": file_name, "mime_type": mime_type,
            "created_at": now_iso(),
        })
        try:
            async def _op(db):
                return await db.posts.insert_one(doc)
            await mongo_db.with_retry(_op)
            return pid
        except Exception:
            return None  # DuplicateKeyError = INSERT OR IGNORE no-op
    try:
        pid = insert(
            "INSERT OR IGNORE INTO posts"
            "(code, kind, media_kind, source_chat_id, source_message_id, "
            " caption, file_id, file_name, mime_type) "
            "VALUES(?, 'cover', ?, ?, ?, ?, ?, ?, ?)",
            (random_code(8), media_kind, source_chat_id, source_message_id,
             caption, file_id, file_name, mime_type),
        )
        return pid or None
    except Exception:
        return None


async def insert_file(source_chat_id: int, source_message_id: int,
                parent_msg_id: int, caption: Optional[str],
                media_kind: str, file_id: Optional[str],
                file_name: Optional[str], mime_type: Optional[str] = None
                ) -> Optional[int]:
    if _mongo():
        from .. import mongo_db
        pid = await _m_next_post_id()
        doc = _drop_nones({
            "_id": pid, "code": random_code(8), "kind": "file",
            "media_kind": media_kind,
            "source_chat_id": int(source_chat_id),
            "source_message_id": int(source_message_id),
            "parent_source_message_id": int(parent_msg_id),
            "caption": caption, "file_id": file_id,
            "file_name": file_name, "mime_type": mime_type,
            "created_at": now_iso(),
        })
        try:
            async def _op(db):
                return await db.posts.insert_one(doc)
            await mongo_db.with_retry(_op)
            return pid
        except Exception:
            return None
    try:
        pid = insert(
            "INSERT OR IGNORE INTO posts"
            "(code, kind, media_kind, source_chat_id, source_message_id, "
            " parent_source_message_id, caption, file_id, file_name, mime_type) "
            "VALUES(?, 'file', ?, ?, ?, ?, ?, ?, ?, ?)",
            (random_code(8), media_kind, source_chat_id, source_message_id,
             parent_msg_id, caption, file_id, file_name, mime_type),
        )
        return pid or None
    except Exception:
        return None


async def insert_batch(rows: list[tuple]) -> int:
    """Batched insert for backfill. Each row is:
    (kind, media_kind, source_chat_id, source_message_id, parent_msg_id,
     caption, file_id, file_name, mime_type)
    Returns rows actually inserted (duplicates excluded) — same contract
    as INSERT OR IGNORE executemany.
    """
    if not rows:
        return 0
    if _mongo():
        from .. import mongo_db
        from pymongo import InsertOne
        from pymongo.errors import BulkWriteError
        first_id = await _m_reserve_post_ids(len(rows))
        ops = []
        for i, r in enumerate(rows):
            (kind, media_kind, s_chat, s_msg, parent, caption,
             file_id, file_name, mime) = r
            doc = _drop_nones({
                "_id": first_id + i, "code": random_code(8),
                "kind": kind, "media_kind": media_kind,
                "source_chat_id": int(s_chat),
                "source_message_id": int(s_msg),
                "parent_source_message_id": int(parent) if parent else None,
                "caption": caption, "file_id": file_id,
                "file_name": file_name, "mime_type": mime,
                "created_at": now_iso(),
            })
            ops.append(InsertOne(doc))
        try:
            async def _op(db):
                return await db.posts.bulk_write(ops, ordered=False)
            res = await mongo_db.with_retry(_op)
            return int(res.inserted_count)
        except BulkWriteError as bwe:
            # DuplicateKey entries are expected (INSERT OR IGNORE semantics);
            # any OTHER write error is real and must surface.
            details = bwe.details or {}
            inserted = int(details.get("nInserted", 0))
            real_errors = [e for e in details.get("writeErrors", [])
                           if e.get("code") != 11000]
            if real_errors:
                raise
            return inserted
    payload = [(random_code(8), *r) for r in rows]
    return executemany(
        "INSERT OR IGNORE INTO posts"
        "(code, kind, media_kind, source_chat_id, source_message_id, "
        " parent_source_message_id, caption, file_id, file_name, mime_type) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        payload,
    )


async def get_post_by_id(pid: int) -> Optional[dict]:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.find_one({"_id": int(pid)})
        return _row(await mongo_db.with_retry(_op), "id", _POST_ROW_FIELDS)
    return query_one("SELECT * FROM posts WHERE id = ?", (pid,))


async def get_post_by_code(code: str) -> Optional[dict]:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.find_one({"code": code})
        return _row(await mongo_db.with_retry(_op), "id", _POST_ROW_FIELDS)
    return query_one("SELECT * FROM posts WHERE code = ?", (code,))


async def get_post_by_number(n: int) -> Optional[dict]:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.find_one(
                {"post_number": int(n), "kind": "cover"})
        return _row(await mongo_db.with_retry(_op), "id", _POST_ROW_FIELDS)
    return query_one("SELECT * FROM posts WHERE post_number = ? AND kind = 'cover'", (n,))


async def find_cover_before(source_chat_id: int, upto_msg_id: int) -> Optional[dict]:
    """Nearest cover with source_message_id <= upto_msg_id."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.find_one(
                {"kind": "cover", "source_chat_id": int(source_chat_id),
                 "source_message_id": {"$lte": int(upto_msg_id)}},
                sort=[("source_message_id", -1)])
        return _row(await mongo_db.with_retry(_op), "id", _POST_ROW_FIELDS)
    return query_one(
        "SELECT * FROM posts WHERE kind = 'cover' AND source_chat_id = ? "
        "AND source_message_id <= ? "
        "ORDER BY source_message_id DESC LIMIT 1",
        (source_chat_id, upto_msg_id),
    )


async def files_of_cover(source_chat_id: int, cover_msg_id: int) -> List[dict]:
    """INDEX HIT on (source_chat_id, parent_source_message_id)."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.posts.find(
                {"kind": "file", "source_chat_id": int(source_chat_id),
                 "parent_source_message_id": int(cover_msg_id)}
            ).sort("source_message_id", 1)
            return await cur.to_list(length=None)
        return _rows(await mongo_db.with_retry(_op), "id", _POST_ROW_FIELDS)
    return query_all(
        "SELECT * FROM posts WHERE kind = 'file' "
        "AND source_chat_id = ? AND parent_source_message_id = ? "
        "ORDER BY source_message_id ASC",
        (source_chat_id, cover_msg_id),
    )


# ---------- Queue ----------
async def next_queued_cover() -> Optional[dict]:
    """The single next cover to publish. INDEX HIT."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.find_one(
                {"kind": "cover", "published_at": None},
                sort=[("source_chat_id", 1), ("source_message_id", 1)])
        return _row(await mongo_db.with_retry(_op), "id", _POST_ROW_FIELDS)
    return query_one(
        "SELECT * FROM posts WHERE kind = 'cover' AND published_at IS NULL "
        "ORDER BY source_chat_id ASC, source_message_id ASC LIMIT 1"
    )


async def next_queued_covers(limit: int = 10) -> List[dict]:
    if _mongo():
        from .. import mongo_db
        proj = {"code": 1, "source_chat_id": 1, "source_message_id": 1, "caption": 1}

        async def _op(db):
            cur = db.posts.find(
                {"kind": "cover", "published_at": None}, proj
            ).sort([("source_chat_id", 1), ("source_message_id", 1)]).limit(int(limit))
            return await cur.to_list(length=None)
        return _rows(await mongo_db.with_retry(_op), "id",
                     ["code", "source_chat_id", "source_message_id", "caption"])
    return query_all(
        "SELECT id, code, source_chat_id, source_message_id, caption "
        "FROM posts WHERE kind = 'cover' AND published_at IS NULL "
        "ORDER BY source_chat_id ASC, source_message_id ASC LIMIT ?",
        (int(limit),),
    )


async def queued_cover_count() -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.count_documents(
                {"kind": "cover", "published_at": None})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'cover' AND published_at IS NULL", (), 0
    ) or 0)


async def published_cover_count() -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.count_documents(
                {"kind": "cover", "published_at": {"$ne": None}})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'cover' AND published_at IS NOT NULL", (), 0
    ) or 0)


async def total_cover_count() -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.count_documents({"kind": "cover"})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'cover'", (), 0
    ) or 0)


async def total_file_count() -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.posts.count_documents({"kind": "file"})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar(
        "SELECT COUNT(*) FROM posts WHERE kind = 'file'", (), 0
    ) or 0)


async def highest_post_number() -> int:
    if _mongo():
        from .. import mongo_db
        return await mongo_db.get_counter("post_number")
    return int(query_scalar(
        "SELECT COALESCE(MAX(post_number), 0) FROM posts WHERE kind = 'cover'", (), 0
    ) or 0)


async def next_post_number() -> int:
    return await highest_post_number() + 1


async def predicted_number_of_next(limit: int = 10) -> List[dict]:
    """Return the next `limit` queued covers with their predicted #N."""
    base = await highest_post_number()
    rows = await next_queued_covers(limit)
    out = []
    for i, r in enumerate(rows, start=1):
        r = dict(r)
        r["predicted_number"] = base + i
        out.append(r)
    return out


async def mark_published(post_id: int, main_chat_id: int, main_message_id: int,
                   file_id: Optional[str] = None) -> int:
    """Assign the next #N atomically, stamp published_at, cache file_id.

    Mongo: the number comes from counters['post_number'] via atomic
    find_one_and_update($inc) — consumed ONLY when the post is not already
    numbered, so reposts never burn a #N (no gaps in the sequence) — the
    same guarantee as the SQL UPDATE-with-subquery COALESCE version.
    """
    if _mongo():
        from .. import mongo_db

        async def _read(db):
            return await db.posts.find_one(
                {"_id": int(post_id)}, {"post_number": 1, "published_at": 1})
        cur_doc = await mongo_db.with_retry(_read)
        if cur_doc is None:
            return 0

        updates: dict = {}
        if cur_doc.get("post_number") is None:
            updates["post_number"] = await _m_next_post_number()
        if cur_doc.get("published_at") is None:
            updates["published_at"] = now_iso()
            updates["main_chat_id"] = int(main_chat_id)
            updates["main_message_id"] = int(main_message_id)
        if file_id:
            updates["file_id"] = file_id  # SQL: COALESCE(?, file_id) — overwrites
        if updates:
            async def _write(db):
                filt = {"_id": int(post_id)}
                if "post_number" in updates:
                    filt["post_number"] = None  # first writer wins the number
                await db.posts.update_one(filt, {"$set": updates})
                return await db.posts.find_one({"_id": int(post_id)},
                                               {"post_number": 1})
            doc = await mongo_db.with_retry(_write)
            return int((doc or {}).get("post_number") or 0)
        return int(cur_doc.get("post_number") or 0)
    execute(
        "UPDATE posts SET "
        "  post_number = COALESCE(post_number, "
        "                          (SELECT COALESCE(MAX(post_number),0)+1 "
        "                           FROM posts WHERE kind='cover')), "
        "  published_at = COALESCE(published_at, ?), "
        "  main_chat_id = COALESCE(main_chat_id, ?), "
        "  main_message_id = COALESCE(main_message_id, ?), "
        "  file_id = COALESCE(?, file_id) "
        "WHERE id = ?",
        (now_iso(), main_chat_id, main_message_id, file_id, post_id),
    )
    row = query_one("SELECT post_number FROM posts WHERE id = ?", (post_id,))
    return int((row or {}).get("post_number") or 0)


async def unpublish(post_id: int) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.posts.update_one(
                {"_id": int(post_id)},
                {"$unset": {"published_at": "", "main_chat_id": "",
                            "main_message_id": "", "post_number": ""}})
        await mongo_db.with_retry(_op)
        return
    execute(
        "UPDATE posts SET published_at = NULL, main_chat_id = NULL, main_message_id = NULL, "
        "post_number = NULL WHERE id = ?", (post_id,))


async def update_file_id(post_id: int, file_id: str) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.posts.update_one({"_id": int(post_id)},
                                      {"$set": {"file_id": file_id}})
        await mongo_db.with_retry(_op)
        return
    execute("UPDATE posts SET file_id = ? WHERE id = ?", (file_id, post_id))


# ---------- Queue-control commands ----------
async def _skip_rows(rows: list, main_chat_id: int) -> int:
    """Shared skip implementation: assign sequential numbers + stamp."""
    if not rows:
        return 0
    stamp = now_iso()
    if _mongo():
        from .. import mongo_db
        base = await _m_reserve_post_numbers(len(rows))
        ids = [int(r["id"]) for r in rows]

        async def _op(db):
            # One update per row — skips are rare admin actions, fine.
            for i, rid in enumerate(ids):
                await db.posts.update_one(
                    {"_id": rid},
                    {"$set": {"post_number": base + i + 1,
                              "published_at": stamp,
                              "main_chat_id": int(main_chat_id),
                              "main_message_id": 0}})
            return len(ids)
        return await mongo_db.with_retry(_op)
    base = await highest_post_number()
    payload = [
        (base + i + 1, stamp, main_chat_id, 0, r["id"])
        for i, r in enumerate(rows)
    ]
    executemany(
        "UPDATE posts SET post_number = ?, published_at = ?, "
        "main_chat_id = ?, main_message_id = ? WHERE id = ?",
        payload,
    )
    return len(rows)


async def skip_first_n(n: int, main_chat_id: int) -> int:
    """Mark the next `n` pending covers as skipped. Used by /skip #N."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.posts.find(
                {"kind": "cover", "published_at": None}, {"_id": 1}
            ).sort([("source_chat_id", 1), ("source_message_id", 1)]).limit(int(n))
            return await cur.to_list(length=None)
        rows = _rows(await mongo_db.with_retry(_op), "id")
        return await _skip_rows(rows, main_chat_id)
    rows = query_all(
        "SELECT id FROM posts WHERE kind='cover' AND published_at IS NULL "
        "ORDER BY source_chat_id ASC, source_message_id ASC LIMIT ?",
        (int(n),),
    )
    return await _skip_rows(rows, main_chat_id)


async def skip_up_to_source(source_chat_id: int, upto_msg_id: int,
                      main_chat_id: int) -> int:
    """Mark every pending cover with source_message_id <= upto_msg_id as
    published-skipped. Used by /skip <link>."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.posts.find(
                {"kind": "cover", "published_at": None,
                 "source_chat_id": int(source_chat_id),
                 "source_message_id": {"$lte": int(upto_msg_id)}}, {"_id": 1}
            ).sort([("source_chat_id", 1), ("source_message_id", 1)])
            return await cur.to_list(length=None)
        rows = _rows(await mongo_db.with_retry(_op), "id")
        return await _skip_rows(rows, main_chat_id)
    rows = query_all(
        "SELECT id FROM posts WHERE kind='cover' AND published_at IS NULL "
        "AND source_chat_id = ? AND source_message_id <= ? "
        "ORDER BY source_chat_id ASC, source_message_id ASC",
        (source_chat_id, upto_msg_id),
    )
    return await _skip_rows(rows, main_chat_id)


async def unskip_by_number(n: int) -> Optional[dict]:
    """Reverse of /skip for a specific #N — returns the affected row."""
    row = await get_post_by_number(n)
    if not row:
        return None
    await unpublish(int(row["id"]))
    return row


async def jumpto_number(n: int) -> int:
    """Force queue cursor back to #N: unpublish #N and every #M > N."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            res = await db.posts.update_many(
                {"kind": "cover", "post_number": {"$ne": None, "$gte": int(n)}},
                {"$unset": {"published_at": "", "main_chat_id": "",
                            "main_message_id": "", "post_number": ""}})
            return res.modified_count
        n_reset = int(await mongo_db.with_retry(_op))
        # Turso recomputes next #N from MAX() — after jumpto that is n-1.
        # Only lower the counter when rows were actually cleared.
        if n_reset:
            cur = await mongo_db.get_counter("post_number")
            if cur >= int(n):
                await mongo_db.set_counter("post_number", int(n) - 1)
        return n_reset
    return execute(
        "UPDATE posts SET published_at = NULL, main_chat_id = NULL, "
        "main_message_id = NULL, post_number = NULL "
        "WHERE kind = 'cover' AND post_number IS NOT NULL AND post_number >= ?",
        (int(n),),
    )


async def queue_reset() -> int:
    """Nuclear: unpublish EVERY cover so drip starts from #1 again."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            res = await db.posts.update_many(
                {"kind": "cover"},
                {"$unset": {"published_at": "", "main_chat_id": "",
                            "main_message_id": "", "post_number": ""}})
            return res.modified_count
        n_reset = int(await mongo_db.with_retry(_op))
        # Turso: next publish = MAX+1 = #1 after a full reset.
        await mongo_db.set_counter("post_number", 0)
        return n_reset
    return execute(
        "UPDATE posts SET published_at = NULL, main_chat_id = NULL, "
        "main_message_id = NULL, post_number = NULL WHERE kind = 'cover'"
    )


async def delete_post_by_number(n: int) -> bool:
    """Soft-delete via kind='skip' so audit is preserved."""
    row = await get_post_by_number(n)
    if not row:
        return False
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.posts.update_one({"_id": int(row["id"])},
                                      {"$set": {"kind": "skip"}})
        await mongo_db.with_retry(_op)
    else:
        execute("UPDATE posts SET kind = 'skip' WHERE id = ?", (int(row["id"]),))
    return True


async def skip_post_by_id(post_id: int) -> None:
    """Remove a post from the queue without consuming a #N (kind='skip').
    Used by the publisher when the source DB-channel message was deleted —
    'message to copy not found' is permanent, so the cover can never publish."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.posts.update_one({"_id": int(post_id)},
                                      {"$set": {"kind": "skip"}})
            return 1
        await mongo_db.with_retry(_op)
        return
    execute("UPDATE posts SET kind = 'skip' WHERE id = ?", (int(post_id),))


async def delete_post_by_code(code: str) -> bool:
    row = await get_post_by_code(code)
    if not row:
        return False
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.posts.update_one({"_id": int(row["id"])},
                                      {"$set": {"kind": "skip"}})
        await mongo_db.with_retry(_op)
    else:
        execute("UPDATE posts SET kind = 'skip' WHERE id = ?", (int(row["id"]),))
    return True


# ---------- Search ----------
async def find_by_caption(pattern: str, limit: int = 20) -> List[dict]:
    if _mongo():
        from .. import mongo_db
        proj = {"post_number": 1, "code": 1, "source_chat_id": 1,
                "source_message_id": 1, "caption": 1}
        rx = re.compile(re.escape(pattern), re.IGNORECASE)

        async def _op(db):
            cur = db.posts.find(
                {"kind": "cover", "caption": {"$regex": rx}}, proj
            ).limit(int(limit) * 4)   # over-fetch; stable-sort below
            return await cur.to_list(length=None)
        docs = _rows(await mongo_db.with_retry(_op), "id")
        docs.sort(key=lambda d: (d.get("post_number")
                                 if d.get("post_number") is not None else 999999))
        return docs[: int(limit)]
    like = f"%{pattern}%"
    return query_all(
        "SELECT id, post_number, code, source_chat_id, source_message_id, caption "
        "FROM posts WHERE kind = 'cover' AND caption LIKE ? "
        "ORDER BY COALESCE(post_number, 999999) ASC LIMIT ?",
        (like, int(limit)),
    )


# ---------- Favorites ----------
async def add_favorite(user_id: int, post_id: int) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.favorites.update_one(
                {"_id": f"{int(user_id)}:{int(post_id)}"},
                {"$setOnInsert": {"user_id": int(user_id),
                                  "post_id": int(post_id),
                                  "saved_at": now_iso()}},
                upsert=True)
        await mongo_db.with_retry(_op)
        return
    execute(
        "INSERT OR IGNORE INTO favorites(user_id, post_id) VALUES(?, ?)",
        (user_id, post_id),
    )


async def remove_favorite(user_id: int, post_id: int) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.favorites.delete_one(
                {"_id": f"{int(user_id)}:{int(post_id)}"})
        await mongo_db.with_retry(_op)
        return
    execute("DELETE FROM favorites WHERE user_id = ? AND post_id = ?",
            (user_id, post_id))


async def _m_saved_posts_with_fav(user_id: int) -> list:
    """Mongo helper: join favorites → saved post docs (small N per user)."""
    from .. import mongo_db

    async def _op(db):
        cur = db.favorites.find({"user_id": int(user_id)}).sort("saved_at", -1)
        favs = await cur.to_list(length=1000)
        if not favs:
            return []
        ids = [int(f["post_id"]) for f in favs]
        pcur = db.posts.find({"_id": {"$in": ids}})
        pmap = {int(d["_id"]): d for d in await pcur.to_list(length=None)}
        out = []
        for f in favs:
            p = pmap.get(int(f["post_id"]))
            if p:
                out.append((f, p))
        return out
    return await mongo_db.with_retry(_op)


def _cover_key_for_saved(p: dict) -> tuple:
    """The (chat, msg) of the cover a saved post resolves to:
    the parent for files, itself for covers — mirrors the SQL COALESCE join."""
    if p.get("kind") == "file" and p.get("parent_source_message_id") is not None:
        return (int(p["source_chat_id"]), int(p["parent_source_message_id"]))
    return (int(p["source_chat_id"]), int(p["source_message_id"]))


async def _m_resolve_covers(keys: list) -> dict:
    from .. import mongo_db
    if not keys:
        return {}

    async def _op(db):
        cur = db.posts.find(
            {"kind": "cover",
             "$or": [{"source_chat_id": c, "source_message_id": m}
                     for (c, m) in keys]})
        docs = await cur.to_list(length=None)
        return {(int(d["source_chat_id"]), int(d["source_message_id"])): d
                for d in docs}
    return await mongo_db.with_retry(_op)


async def list_favorites(user_id: int) -> List[dict]:
    """Return the user's saved posts resolved to their parent COVER.

    Same contract as the SQL JOIN version: one row per cover (even if the
    user saved 3 files of it), cover fields plus fav_post_id + saved_at of
    the most recent save, ordered by last save DESC, capped at 100.
    """
    if _mongo():
        pairs = await _m_saved_posts_with_fav(user_id)
        if not pairs:
            return []
        keys = list({_cover_key_for_saved(p) for (_f, p) in pairs})
        covers = await _m_resolve_covers(keys)
        grouped: dict = {}
        for f, p in pairs:
            cdoc = covers.get(_cover_key_for_saved(p))
            if not cdoc:
                continue
            cid = int(cdoc["_id"])
            row = grouped.get(cid)
            if row is None:
                row = _row(cdoc, "id", _POST_ROW_FIELDS)
                row["fav_post_id"] = int(f["post_id"])
                row["saved_at"] = f.get("saved_at")
                grouped[cid] = row
            else:
                if (f.get("saved_at") or "") > (row.get("saved_at") or ""):
                    row["saved_at"] = f.get("saved_at")
                    row["fav_post_id"] = int(f["post_id"])
        out = list(grouped.values())
        out.sort(key=lambda r: r.get("saved_at") or "", reverse=True)
        return out[:100]
    return query_all(
        """SELECT c.*, f.post_id AS fav_post_id, f.saved_at
           FROM favorites f
           JOIN posts p  ON p.id = f.post_id
           JOIN posts c  ON c.kind = 'cover'
                        AND c.source_chat_id    = p.source_chat_id
                        AND c.source_message_id = COALESCE(
                              CASE WHEN p.kind = 'file'
                                   THEN p.parent_source_message_id END,
                              p.source_message_id)
           WHERE f.user_id = ?
           GROUP BY c.id
           ORDER BY MAX(f.saved_at) DESC
           LIMIT 100""",
        (user_id,),
    )


async def is_favorite(user_id: int, post_id: int) -> bool:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.favorites.find_one(
                {"_id": f"{int(user_id)}:{int(post_id)}"}, {"_id": 1})
        return (await mongo_db.with_retry(_op)) is not None
    return query_one(
        "SELECT 1 FROM favorites WHERE user_id = ? AND post_id = ? LIMIT 1",
        (user_id, post_id),
    ) is not None


async def remove_favorites_for_cover(user_id: int, source_chat_id: int,
                               cover_msg_id: int) -> int:
    """Delete every favorite of this user that belongs to the given cover."""
    if _mongo():
        from .. import mongo_db

        async def _find(db):
            cur = db.posts.find(
                {"source_chat_id": int(source_chat_id),
                 "$or": [{"source_message_id": int(cover_msg_id)},
                         {"parent_source_message_id": int(cover_msg_id)}]},
                {"_id": 1})
            return [int(d["_id"]) for d in await cur.to_list(length=None)]
        ids = await mongo_db.with_retry(_find)
        if not ids:
            return 0

        async def _del(db):
            res = await db.favorites.delete_many(
                {"user_id": int(user_id), "post_id": {"$in": ids}})
            return res.deleted_count
        return int(await mongo_db.with_retry(_del))
    return execute(
        """DELETE FROM favorites
           WHERE user_id = ?
             AND post_id IN (
                 SELECT id FROM posts
                 WHERE source_chat_id = ?
                   AND (source_message_id = ?
                        OR parent_source_message_id = ?))""",
        (user_id, source_chat_id, cover_msg_id, cover_msg_id),
    )


# ---------- user directory (powers /favsall names) ----------
async def upsert_directory_user(user_id: int, username: Optional[str],
                          first_name: Optional[str]) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.user_directory.update_one(
                {"_id": int(user_id)},
                {"$set": {"user_id": int(user_id),
                          "username": username,
                          "first_name": first_name,
                          "updated_at": now_iso()}},
                upsert=True)
        await mongo_db.with_retry(_op)
        return
    execute(
        "INSERT INTO user_directory(user_id, username, first_name) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, "
        "first_name=excluded.first_name, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (user_id, username, first_name),
    )


async def get_directory_users(user_ids: List[int]) -> dict:
    if not user_ids:
        return {}
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.user_directory.find(
                {"_id": {"$in": [int(u) for u in user_ids]}})
            return await cur.to_list(length=None)
        rows = _rows(await mongo_db.with_retry(_op), "user_id",
                     ["username", "first_name"])
        return {int(r["user_id"]): r for r in rows}
    marks = ",".join("?" for _ in user_ids)
    rows = query_all(
        f"SELECT user_id, username, first_name FROM user_directory "
        f"WHERE user_id IN ({marks})", tuple(user_ids))
    return {int(r["user_id"]): r for r in rows}


# ---------- /favsall aggregates ----------
async def top_savers(limit: int = 100, offset: int = 0) -> List[dict]:
    """Users ranked by save count (page of `limit`)."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            pipeline = [
                {"$group": {"_id": "$user_id",
                            "saves": {"$sum": 1},
                            "last_save": {"$max": "$saved_at"}}},
                {"$sort": {"saves": -1, "last_save": -1}},
                {"$skip": int(offset)},
                {"$limit": int(limit)},
            ]
            cur = await db.favorites.aggregate(pipeline)
            return await cur.to_list(length=None)
        rows = await mongo_db.with_retry(_op)
        return [{"user_id": int(r["_id"]), "saves": r["saves"],
                 "last_save": r.get("last_save")} for r in rows]
    return query_all(
        """SELECT f.user_id, COUNT(*) AS saves, MAX(f.saved_at) AS last_save
           FROM favorites f
           GROUP BY f.user_id
           ORDER BY saves DESC, last_save DESC
           LIMIT ? OFFSET ?""",
        (int(limit), int(offset)),
    )


async def savers_total() -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return len(await db.favorites.distinct("user_id"))
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar("SELECT COUNT(DISTINCT user_id) FROM favorites", (), 0) or 0)


async def saves_total() -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.favorites.count_documents({})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar("SELECT COUNT(*) FROM favorites", (), 0) or 0)


async def favorite_covers_of_user(user_id: int, limit: int = 3) -> List[dict]:
    """The user's saved posts resolved to their parent covers (titles)."""
    if _mongo():
        pairs = await _m_saved_posts_with_fav(user_id)
        if not pairs:
            return []
        keys = list({_cover_key_for_saved(p) for (_f, p) in pairs})
        covers = await _m_resolve_covers(keys)
        seen: set = set()
        out: list = []
        for f, p in pairs:  # pairs ordered by saved_at DESC
            cdoc = covers.get(_cover_key_for_saved(p))
            if not cdoc:
                continue
            cid = int(cdoc["_id"])
            if cid in seen:
                continue
            seen.add(cid)
            out.append({"caption": cdoc.get("caption"),
                        "post_number": cdoc.get("post_number"),
                        "id": cid})
            if len(out) >= int(limit):
                break
        return out
    return query_all(
        """SELECT DISTINCT c.caption, c.post_number, c.id
           FROM favorites f
           JOIN posts p ON p.id = f.post_id
           JOIN posts c ON c.kind = 'cover'
                       AND c.source_chat_id = p.source_chat_id
                       AND c.source_message_id = COALESCE(
                             CASE WHEN p.kind = 'file'
                                  THEN p.parent_source_message_id END,
                             p.source_message_id)
           WHERE f.user_id = ?
           ORDER BY f.saved_at DESC
           LIMIT ?""",
        (user_id, int(limit)),
    )


async def favorites_count_of_user(user_id: int) -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.favorites.count_documents({"user_id": int(user_id)})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar(
        "SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,), 0) or 0)


# ============================================================================
# Backup channel mirroring (v2.9)
# ============================================================================
async def backup_is_paused() -> bool:
    return await get_setting_bool("backup_paused", False)


async def set_backup_paused(paused: bool) -> None:
    await set_setting("backup_paused", "1" if paused else None)


async def all_db_source_messages() -> List[dict]:
    """Every non-skipped post in every DB channel, channel order."""
    if _mongo():
        from .. import mongo_db
        proj = {"source_chat_id": 1, "source_message_id": 1, "kind": 1}

        async def _op(db):
            cur = db.posts.find(
                {"kind": {"$in": ["cover", "file"]}}, proj
            ).sort([("source_chat_id", 1), ("source_message_id", 1)])
            return await cur.to_list(length=None)
        return _rows(await mongo_db.with_retry(_op), "id")
    return query_all(
        "SELECT id, source_chat_id, source_message_id, kind "
        "FROM posts WHERE kind IN ('cover','file') "
        "ORDER BY source_chat_id ASC, source_message_id ASC"
    )


async def backup_mirrored_set(backup_chat_id: int) -> set:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.backup_progress.find(
                {"backup_chat_id": int(backup_chat_id)},
                {"db_chat_id": 1, "source_message_id": 1})
            return await cur.to_list(length=None)
        rows = await mongo_db.with_retry(_op)
        return {(int(r["db_chat_id"]), int(r["source_message_id"])) for r in rows}
    rows = query_all(
        "SELECT db_chat_id, source_message_id FROM backup_progress "
        "WHERE backup_chat_id = ?", (int(backup_chat_id),))
    return {(int(r["db_chat_id"]), int(r["source_message_id"])) for r in rows}


async def backup_mirrored_count(backup_chat_id: int) -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.backup_progress.count_documents(
                {"backup_chat_id": int(backup_chat_id)})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar(
        "SELECT COUNT(*) FROM backup_progress WHERE backup_chat_id = ?",
        (int(backup_chat_id),), 0) or 0)


async def backup_record(backup_chat_id: int, db_chat_id: int,
                  source_message_id: int, target_message_id) -> None:
    if _mongo():
        from .. import mongo_db
        _id = f"{int(backup_chat_id)}:{int(db_chat_id)}:{int(source_message_id)}"

        async def _op(db):
            await db.backup_progress.update_one(
                {"_id": _id},
                {"$set": {"backup_chat_id": int(backup_chat_id),
                          "db_chat_id": int(db_chat_id),
                          "source_message_id": int(source_message_id),
                          "target_message_id": target_message_id},
                 "$setOnInsert": {"mirrored_at": now_iso()}},
                upsert=True)
        await mongo_db.with_retry(_op)
        return
    execute(
        "INSERT OR REPLACE INTO backup_progress"
        "(backup_chat_id, db_chat_id, source_message_id, target_message_id) "
        "VALUES(?,?,?,?)",
        (int(backup_chat_id), int(db_chat_id),
         int(source_message_id), target_message_id),
    )


async def backup_reset(backup_chat_id: int) -> int:
    """Archive current progress to history, then clear. Returns rows moved."""
    backup_chat_id = int(backup_chat_id)
    if _mongo():
        from .. import mongo_db

        async def _read(db):
            cur = db.backup_progress.find({"backup_chat_id": backup_chat_id})
            return await cur.to_list(length=None)
        rows = await mongo_db.with_retry(_read)
        if rows:
            stamp = now_iso()
            # Allocate history ids so verification stays deterministic.
            first_hid = (await mongo_db.next_counter("backup_history_id"))
            # next_counter returned +1 for us; allocate the rest as a block
            more = len(rows) - 1
            if more > 0:
                async def _bump(db):
                    await db.counters.update_one(
                        {"_id": "backup_history_id"},
                        {"$inc": {"seq": more}}, upsert=True)
                await mongo_db.with_retry(_bump)
            hist = []
            for i, r in enumerate(rows):
                hist.append({
                    "_id": first_hid + i,
                    "backup_chat_id": backup_chat_id,
                    "db_chat_id": int(r["db_chat_id"]),
                    "source_message_id": int(r["source_message_id"]),
                    "target_message_id": r.get("target_message_id"),
                    "reset_at": stamp})

            async def _write(db):
                await db.backup_history.insert_many(hist, ordered=False)
                await db.backup_progress.delete_many(
                    {"backup_chat_id": backup_chat_id})
                return True
            await mongo_db.with_retry(_write)
        return len(rows)
    rows = query_all(
        "SELECT db_chat_id, source_message_id, target_message_id "
        "FROM backup_progress WHERE backup_chat_id = ?", (backup_chat_id,))
    if rows:
        payload = [(backup_chat_id, int(x["db_chat_id"]),
                    int(x["source_message_id"]), x.get("target_message_id"))
                   for x in rows]
        executemany(
            "INSERT INTO backup_history"
            "(backup_chat_id, db_chat_id, source_message_id, target_message_id) "
            "VALUES(?,?,?,?)", payload)
    execute("DELETE FROM backup_progress WHERE backup_chat_id = ?",
            (backup_chat_id,))
    return len(rows)


async def backup_undo_reset(backup_chat_id: int) -> int:
    """Restore the most recently-reset batch from history."""
    backup_chat_id = int(backup_chat_id)
    if _mongo():
        from .. import mongo_db

        async def _latest(db):
            return await db.backup_history.find_one(
                {"backup_chat_id": backup_chat_id},
                sort=[("reset_at", -1)])
        top = await mongo_db.with_retry(_latest)
        ts = (top or {}).get("reset_at")
        if not ts:
            return 0

        async def _rows_op(db):
            cur = db.backup_history.find(
                {"backup_chat_id": backup_chat_id, "reset_at": ts})
            return await cur.to_list(length=None)
        rows = await mongo_db.with_retry(_rows_op)
        if not rows:
            return 0
        from pymongo import UpdateOne
        ops = [UpdateOne(
            {"_id": f"{backup_chat_id}:{int(x['db_chat_id'])}:{int(x['source_message_id'])}"},
            {"$set": {"backup_chat_id": backup_chat_id,
                      "db_chat_id": int(x["db_chat_id"]),
                      "source_message_id": int(x["source_message_id"]),
                      "target_message_id": x.get("target_message_id"),
                      "mirrored_at": now_iso()}},
            upsert=True) for x in rows]

        async def _write(db):
            await db.backup_progress.bulk_write(ops, ordered=False)
            await db.backup_history.delete_many(
                {"backup_chat_id": backup_chat_id, "reset_at": ts})
            return True
        await mongo_db.with_retry(_write)
        return len(rows)
    row = query_one(
        "SELECT MAX(reset_at) AS ts FROM backup_history WHERE backup_chat_id = ?",
        (backup_chat_id,))
    ts = (row or {}).get("ts")
    if not ts:
        return 0
    rows = query_all(
        "SELECT db_chat_id, source_message_id, target_message_id "
        "FROM backup_history WHERE backup_chat_id = ? AND reset_at = ?",
        (backup_chat_id, ts))
    if not rows:
        return 0
    payload = [(backup_chat_id, int(x["db_chat_id"]),
                int(x["source_message_id"]), x.get("target_message_id"))
               for x in rows]
    executemany(
        "INSERT OR REPLACE INTO backup_progress"
        "(backup_chat_id, db_chat_id, source_message_id, target_message_id) "
        "VALUES(?,?,?,?)", payload)
    execute("DELETE FROM backup_history WHERE backup_chat_id = ? AND reset_at = ?",
            (backup_chat_id, ts))
    return len(rows)


async def backup_delete_all_progress(backup_chat_id: int) -> int:
    """For /dltbackup: remove progress + history rows for that channel."""
    backup_chat_id = int(backup_chat_id)
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            res = await db.backup_progress.delete_many(
                {"backup_chat_id": backup_chat_id})
            await db.backup_history.delete_many({"backup_chat_id": backup_chat_id})
            return res.deleted_count
        return int(await mongo_db.with_retry(_op))
    n = execute("DELETE FROM backup_progress WHERE backup_chat_id = ?",
                (backup_chat_id,))
    execute("DELETE FROM backup_history WHERE backup_chat_id = ?",
            (backup_chat_id,))
    return n


# ============================================================================
# Raw passthrough (one legacy spot: /broadcast user list)
# ============================================================================
async def all_user_ids() -> List[int]:
    """Replaces the raw `repo.query_all("SELECT user_id FROM user_directory")`."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.user_directory.find({}, {"_id": 1})
            return [int(d["_id"]) for d in await cur.to_list(length=None)]
        return await mongo_db.with_retry(_op)
    rows = query_all("SELECT user_id FROM user_directory")
    return [int(r["user_id"]) for r in rows]



# ============================================================================
# fsub join-request tracking (v3.3.1)
# A user who REQUESTED to join a private approval-gated fsub channel is not a
# member yet (get_chat_member says 'left' / 'user not found'). ChatJoinRequest
# events are recorded here; the gate treats a recorded request as passing.
# ============================================================================
_FSUB_REQ_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS fsub_requests ("
    " chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,"
    " requested_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),"
    " PRIMARY KEY (chat_id, user_id))"
)
_fsub_req_ready = False


def _ensure_fsub_req_table() -> None:
    global _fsub_req_ready
    if _fsub_req_ready:
        return
    execute(_FSUB_REQ_TABLE_SQL)
    _fsub_req_ready = True


async def add_fsub_request(chat_id: int, user_id: int) -> None:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.fsub_requests.update_one(
                {"_id": f"{int(chat_id)}:{int(user_id)}"},
                {"$set": {"chat_id": int(chat_id), "user_id": int(user_id),
                          "requested_at": now_iso()}},
                upsert=True)
            return 1
        await mongo_db.with_retry(_op)
        return
    _ensure_fsub_req_table()
    execute("INSERT OR REPLACE INTO fsub_requests (chat_id, user_id) VALUES (?, ?)",
            (int(chat_id), int(user_id)))


async def has_fsub_request(chat_id: int, user_id: int) -> bool:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            d = await db.fsub_requests.find_one(
                {"_id": f"{int(chat_id)}:{int(user_id)}"}, {"_id": 1})
            return bool(d)
        return bool(await mongo_db.with_retry(_op))
    _ensure_fsub_req_table()
    row = query_one("SELECT 1 AS ok FROM fsub_requests WHERE chat_id = ? AND user_id = ?",
                    (int(chat_id), int(user_id)))
    return bool(row)


async def remove_fsub_request(chat_id: int, user_id: int) -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            res = await db.fsub_requests.delete_one(
                {"_id": f"{int(chat_id)}:{int(user_id)}"})
            return res.deleted_count
        return int(await mongo_db.with_retry(_op))
    _ensure_fsub_req_table()
    return execute("DELETE FROM fsub_requests WHERE chat_id = ? AND user_id = ?",
                   (int(chat_id), int(user_id)))


async def remove_fsub_requests_for_user(user_id: int) -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            res = await db.fsub_requests.delete_many({"user_id": int(user_id)})
            return res.deleted_count
        return int(await mongo_db.with_retry(_op))
    _ensure_fsub_req_table()
    return execute("DELETE FROM fsub_requests WHERE user_id = ?", (int(user_id),))


# ============================================================================
# User stats tracking (v3.4) — powers the enriched /stats
# ============================================================================
_UD_STATS_SQL = """CREATE TABLE IF NOT EXISTS user_directory_stats (
  user_id    INTEGER PRIMARY KEY,
  first_seen TEXT,
  last_seen  TEXT
)"""
_UD_STATS_READY = False


def _ensure_ud_stats() -> None:
    global _UD_STATS_READY
    if _UD_STATS_READY:
        return
    execute(_UD_STATS_SQL)
    _UD_STATS_READY = True


async def track_user_seen(user_id: int, username: Optional[str] = None,
                          first_name: Optional[str] = None) -> None:
    """Upsert identity + record first/last seen + bump activity counters.
    Called on /start and on every delivery — cheap, one upsert each."""
    from ..utils import today_ist, week_start_ist, month_start_ist
    uid = int(user_id)
    await upsert_directory_user(uid, username, first_name)
    today, week, month = today_ist(), week_start_ist(), month_start_ist()
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.user_directory_stats.update_one(
                {"_id": uid},
                {"$setOnInsert": {"first_seen": now_iso()},
                 "$set": {"last_seen": now_iso()}},
                upsert=True)
            # v3.8: per-period _id — a FIXED _id + day/week/month in the filter
            # upserted a duplicate _id after rollover (DuplicateKeyError,
            # silently swallowed) and froze every daily/weekly counter.
            await db.usage_counters.update_one(
                {"_id": f"active_today:{today}"},
                {"$set": {"day": today}, "$addToSet": {"uids": uid}},
                upsert=True)
            await db.usage_counters.update_one(
                {"_id": f"active_week:{week}"},
                {"$set": {"week": week}, "$addToSet": {"uids": uid}},
                upsert=True)
            await db.usage_counters.update_one(
                {"_id": f"active_month:{month}"},
                {"$set": {"month": month}, "$addToSet": {"uids": uid}},
                upsert=True)
            return 1
        await mongo_db.with_retry(_op)
        return
    _ensure_ud_stats()
    execute("INSERT INTO user_directory_stats(user_id, first_seen, last_seen) "
            "VALUES(?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "last_seen=excluded.last_seen", (uid, now_iso(), now_iso()))
    for key, val in (("active_today", today), ("active_week", week),
                     ("active_month", month)):
        rows = await get_setting_json(f"uc_{key}", {"period": val, "uids": []})
        if rows.get("period") != val:
            rows = {"period": val, "uids": []}
        if uid not in rows["uids"]:
            rows["uids"].append(uid)
        await set_setting_json(f"uc_{key}", rows)


async def record_file_fetch(user_id: int, count: int) -> None:
    from ..utils import today_ist
    uid, today = int(user_id), today_ist()
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.usage_counters.update_one(
                {"_id": f"fetches_today:{today}"},
                {"$set": {"day": today}, "$inc": {"n": int(count)}},
                upsert=True)
            await db.usage_counters.update_one(
                {"_id": "fetches_total"},
                {"$inc": {"n": int(count)}}, upsert=True)
            return 1
        await mongo_db.with_retry(_op)
        return
    cur = await get_setting_json(f"uc_fetches_{today}", {"n": 0})
    cur["n"] = int(cur.get("n", 0)) + int(count)
    await set_setting_json(f"uc_fetches_{today}", cur)
    tot = await get_setting_json("uc_fetches_total", {"n": 0})
    tot["n"] = int(tot.get("n", 0)) + int(count)
    await set_setting_json("uc_fetches_total", tot)


async def _counter_count(coll_key: str, period_field: str, period_val: str,
                         settings_key: str) -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            d = await db.usage_counters.find_one(
                {"_id": f"{coll_key}:{period_val}"})
            return len(d.get("uids", [])) if d else 0
        return int(await mongo_db.with_retry(_op))
    rows = await get_setting_json(settings_key, {"period": period_val, "uids": []})
    return len(rows.get("uids", [])) if rows.get("period") == period_val else 0


async def _counter_sum(coll_key: str, period_field: str, period_val,
                       settings_key: str) -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            if period_field is None:
                d = await db.usage_counters.find_one({"_id": coll_key})
            else:
                d = await db.usage_counters.find_one(
                    {"_id": f"{coll_key}:{period_val}"})
            return int(d.get("n", 0)) if d else 0
        return int(await mongo_db.with_retry(_op))
    rows = await get_setting_json(settings_key, {"n": 0})
    return int(rows.get("n", 0))


async def users_total() -> int:
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.user_directory.count_documents({})
        return int(await mongo_db.with_retry(_op))
    return int(query_scalar("SELECT COUNT(*) FROM user_directory", (), 0) or 0)


async def users_active_today() -> int:
    from ..utils import today_ist
    return await _counter_count("active_today", "day", today_ist(), f"uc_active_today")


async def users_active_week() -> int:
    from ..utils import week_start_ist
    return await _counter_count("active_week", "week", week_start_ist(), f"uc_active_week")


async def users_active_month() -> int:
    from ..utils import month_start_ist
    return await _counter_count("active_month", "month", month_start_ist(), f"uc_active_month")


async def users_new_today() -> int:
    from ..utils import today_ist
    prefix = today_ist()
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            return await db.user_directory_stats.count_documents(
                {"first_seen": {"$regex": f"^{prefix}"}})
        return int(await mongo_db.with_retry(_op))
    _ensure_ud_stats()
    return int(query_scalar(
        "SELECT COUNT(*) FROM user_directory_stats WHERE first_seen LIKE ?",
        (prefix + "%",), 0) or 0)


async def fetches_today() -> int:
    from ..utils import today_ist
    return await _counter_sum("fetches_today", "day", today_ist(), f"uc_fetches_{today_ist()}")


async def fetches_total() -> int:
    return await _counter_sum("fetches_total", None, None, "uc_fetches_total")


# ============================================================================
# Weekly fetch leaderboard (v3.5) — resets automatically by week-period key
# (Monday 00:00 IST rollover; the public /leaderboard shows it as Monday 1 AM)
# ============================================================================
async def record_fetch_weekly(user_id: int, count: int) -> None:
    from ..utils import week_start_ist
    uid, week = int(user_id), week_start_ist()
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            await db.usage_counters.update_one(
                {"_id": f"fetch_week:{week}"},
                {"$set": {"week": week},
                 "$inc": {f"counts.{uid}": int(count)}},
                upsert=True)
            return 1
        await mongo_db.with_retry(_op)
        return
    rows = await get_setting_json("uc_fetchweek", {"period": week, "counts": {}})
    if rows.get("period") != week:
        rows = {"period": week, "counts": {}}
    counts = rows.setdefault("counts", {})
    counts[str(uid)] = int(counts.get(str(uid), 0)) + int(count)
    await set_setting_json("uc_fetchweek", rows)


async def _fetch_week_counts() -> dict:
    """Raw {user_id(str): fetch_count} map for the current IST week."""
    from ..utils import week_start_ist
    week = week_start_ist()
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            d = await db.usage_counters.find_one({"_id": f"fetch_week:{week}"})
            return (d or {}).get("counts", {}) or {}
        return await mongo_db.with_retry(_op)
    rows = await get_setting_json("uc_fetchweek", {"period": week, "counts": {}})
    return rows.get("counts", {}) if rows.get("period") == week else {}


async def top_fetchers_week(limit: int = 10) -> List[dict]:
    counts = await _fetch_week_counts()
    pairs = sorted(((int(u), int(c)) for u, c in counts.items()),
                   key=lambda x: -x[1])
    return [{"user_id": u, "fetches": c} for u, c in pairs[:int(limit)]]


async def fetch_rank_week(user_id: int) -> tuple:
    """(rank, weekly_fetches) for one user. rank=0 when no fetches this week.
    Ties share the rank of the first of them (standard competition ranking)."""
    counts = await _fetch_week_counts()
    mine = int(counts.get(str(int(user_id)), 0))
    if mine <= 0:
        return (0, 0)
    higher = sum(1 for c in counts.values() if int(c) > mine)
    return (higher + 1, mine)


# ============================================================================
# Per-user recommendation preference (v4.1) — /similar on|off (default ON)
# ============================================================================
async def get_similar_pref(user_id: int) -> bool:
    """True = user receives 'Similar Doujinshi' recommendations (default)."""
    rows = await get_setting_json("similar_prefs", {"off": []})
    off = {int(x) for x in rows.get("off", [])}
    return int(user_id) not in off


async def set_similar_pref(user_id: int, on: bool) -> None:
    rows = await get_setting_json("similar_prefs", {"off": []})
    off = {int(x) for x in rows.get("off", [])}
    if on:
        off.discard(int(user_id))
    else:
        off.add(int(user_id))
    await set_setting_json("similar_prefs", {"off": sorted(off)})


# ============================================================================
# Recommendations (v4.0) — candidate pool for /similar buttons
# ============================================================================
async def recent_published_covers(limit: int = 400, exclude_id: int = 0) -> List[dict]:
    """Most recent published covers (have post_number + code) — the pool the
    recommender ranks. Bounded to keep per-delivery cost small."""
    if _mongo():
        from .. import mongo_db

        async def _op(db):
            cur = db.posts.find(
                {"kind": "cover", "published_at": {"$ne": None},
                 "_id": {"$ne": int(exclude_id)}},
                {"_id": 1, "code": 1, "caption": 1, "post_number": 1},
            ).sort("post_number", -1).limit(int(limit))
            return await cur.to_list(length=None)
        rows = await mongo_db.with_retry(_op)
        return [_row(r) for r in rows]
    rows = query_all(
        "SELECT id, code, caption, post_number FROM posts "
        "WHERE kind='cover' AND published_at IS NOT NULL AND id != ? "
        "ORDER BY post_number DESC LIMIT ?",
        (int(exclude_id), int(limit)),
    )
    return rows


# ============================================================================
# v4.3.1: persistent shortener verification stamps (restart-proof).
# Mongo: `verified_users` collection. Turso/dormant: JSON in settings (works
# for a few hundred users — fine as a dormant fallback path).
# ============================================================================
async def verified_get(user_id: int):
    """Return the verified-until epoch (float) for a user, or None."""
    uid = int(user_id)
    if _mongo():
        from .. import mongo_db
        async def _op(db):
            d = await db.verified_users.find_one({"_id": uid}, {"until": 1})
            return float(d["until"]) if d and d.get("until") is not None else None
        try:
            return await mongo_db.with_retry(_op)
        except Exception:
            return None
    data = await get_setting_json("verified_users", {}) or {}
    v = data.get(str(uid))
    return float(v) if v is not None else None


async def verified_set(user_id: int, until_epoch: float) -> None:
    uid = int(user_id)
    if _mongo():
        from .. import mongo_db
        async def _op(db):
            await db.verified_users.update_one(
                {"_id": uid}, {"$set": {"until": float(until_epoch)}}, upsert=True)
            return True
        await mongo_db.with_retry(_op)
        return
    data = await get_setting_json("verified_users", {}) or {}
    data[str(uid)] = float(until_epoch)
    await set_setting_json("verified_users", data)


async def verified_count() -> int:
    if _mongo():
        from .. import mongo_db
        import time as _t
        async def _op(db):
            return await db.verified_users.count_documents({"until": {"$gt": _t.time()}})
        try:
            return await mongo_db.with_retry(_op)
        except Exception:
            return 0
    import time as _t
    data = await get_setting_json("verified_users", {}) or {}
    return sum(1 for v in data.values() if float(v) > _t.time())
