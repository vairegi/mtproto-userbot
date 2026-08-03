"""
db.py — MongoDB access for the Mini App backend.

Reuses the same Mongo cluster as admin_bot.py. Collections are namespaced
`miniapp_*` so they can't collide with the bot's existing collections.

Collections:
    miniapp_settings      { _id: "singleton", public_mode, default_daily,
                            default_cooldown_s }
    miniapp_users         { _id: <user_id>, first_name, username, photo_url,
                            daily_limit (nullable → uses default), banned,
                            first_seen, last_seen }
    miniapp_usage         { _id: <user_id>_<yyyy-mm-dd>, count }
    miniapp_bookmarks     { _id: <user_id>_<gallery_id>, user_id, gallery_id,
                            title, cover, pages, tags, created_at }
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

from pymongo import MongoClient
from pymongo.collection import Collection
from pymongo.database import Database

from .config import settings

_client: Optional[MongoClient] = None


def client() -> MongoClient:
    global _client
    if _client is None:
        if not settings.mongo_uri:
            raise RuntimeError("MONGO_URI is not set")
        _client = MongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    return _client


def db() -> Database:
    return client()[settings.mongo_db]


# ---- Collection accessors ------------------------------------------------
def col_settings() -> Collection:  return db()["miniapp_settings"]
def col_users() -> Collection:     return db()["miniapp_users"]
def col_usage() -> Collection:     return db()["miniapp_usage"]
def col_bookmarks() -> Collection: return db()["miniapp_bookmarks"]


# ---- Settings singleton --------------------------------------------------
def get_setting(key: str, default: Any = None) -> Any:
    doc = col_settings().find_one({"_id": "singleton"}) or {}
    return doc.get(key, default)


def set_setting(key: str, value: Any) -> None:
    col_settings().update_one(
        {"_id": "singleton"},
        {"$set": {key: value}},
        upsert=True,
    )


def get_public_mode() -> bool:
    return bool(get_setting("public_mode", settings.default_public_mode))


def get_default_daily() -> int:
    return int(get_setting("default_daily_limit", settings.default_daily_limit))


def get_default_cooldown() -> int:
    return int(get_setting("default_cooldown_s", settings.default_cooldown_s))


# ---- User records --------------------------------------------------------
def upsert_user(tg_user: dict) -> dict:
    """Called on every authenticated request. Cheap upsert of profile fields."""
    now = _dt.datetime.utcnow()
    uid = int(tg_user["id"])
    upd = {
        "first_name": tg_user.get("first_name"),
        "last_name":  tg_user.get("last_name"),
        "username":   tg_user.get("username"),
        "photo_url":  tg_user.get("photo_url"),
        "language_code": tg_user.get("language_code"),
        "last_seen":  now,
    }
    col_users().update_one(
        {"_id": uid},
        {"$set": upd, "$setOnInsert": {"first_seen": now, "banned": False}},
        upsert=True,
    )
    return col_users().find_one({"_id": uid}) or {}


def is_banned(user_id: int) -> bool:
    doc = col_users().find_one({"_id": int(user_id)}, {"banned": 1}) or {}
    return bool(doc.get("banned"))


def set_banned(user_id: int, banned: bool) -> None:
    col_users().update_one({"_id": int(user_id)},
                           {"$set": {"banned": bool(banned)}}, upsert=True)


def get_user_daily_limit(user_id: int) -> int:
    """Per-user override wins; else fall back to the global default."""
    doc = col_users().find_one({"_id": int(user_id)}, {"daily_limit": 1}) or {}
    override = doc.get("daily_limit")
    if isinstance(override, int) and override >= 0:
        return override
    return get_default_daily()


def set_user_daily_limit(user_id: int, daily: int) -> None:
    col_users().update_one(
        {"_id": int(user_id)},
        {"$set": {"daily_limit": int(daily)}},
        upsert=True,
    )


def list_users(limit: int = 100) -> list[dict]:
    return list(col_users().find({}, limit=limit).sort("last_seen", -1))


# ---- Usage counters ------------------------------------------------------
def _today_key(user_id: int) -> str:
    d = _dt.datetime.utcnow().date().isoformat()
    return f"{int(user_id)}_{d}"


def get_used_today(user_id: int) -> int:
    doc = col_usage().find_one({"_id": _today_key(user_id)}) or {}
    return int(doc.get("count", 0))


def increment_used_today(user_id: int) -> int:
    r = col_usage().find_one_and_update(
        {"_id": _today_key(user_id)},
        {"$inc": {"count": 1},
         "$setOnInsert": {"user_id": int(user_id),
                          "date": _dt.datetime.utcnow().date().isoformat()}},
        upsert=True,
        return_document=True,
    )
    return int(r.get("count", 1))


def reset_used_today(user_id: int) -> None:
    col_usage().delete_one({"_id": _today_key(user_id)})


# ---- Bookmarks -----------------------------------------------------------
def _bm_key(user_id: int, gallery_id: Any) -> str:
    return f"{int(user_id)}_{gallery_id}"


def add_bookmark(user_id: int, g: dict) -> None:
    col_bookmarks().update_one(
        {"_id": _bm_key(user_id, g["id"])},
        {"$set": {
            "user_id": int(user_id),
            "gallery_id": g["id"],
            "title": g.get("title"),
            "cover": g.get("cover"),
            "pages": g.get("pages"),
            "tags":  g.get("tags") or [],
            "created_at": _dt.datetime.utcnow(),
        }},
        upsert=True,
    )


def remove_bookmark(user_id: int, gallery_id: Any) -> None:
    col_bookmarks().delete_one({"_id": _bm_key(user_id, gallery_id)})


def list_bookmarks(user_id: int, limit: int = 200) -> list[dict]:
    cur = col_bookmarks().find({"user_id": int(user_id)}).sort("created_at", -1).limit(limit)
    return list(cur)
