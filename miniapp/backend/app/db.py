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
# v11.7 additions
def col_ratings() -> Collection:   return db()["miniapp_ratings"]      # star ratings
def col_shares()  -> Collection:   return db()["miniapp_shares"]        # share-link events
def col_stats()   -> Collection:   return db()["miniapp_user_stats"]    # per-user counters


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


# ---- v11.7 Ratings -------------------------------------------------------
def _rate_key(user_id: int, gallery_id: Any) -> str:
    return f"{int(user_id)}_{gallery_id}"


def set_rating(user_id: int, gallery_id: Any, stars: int) -> None:
    """Record a 1..5-star rating for a gallery. Overwrites any previous vote."""
    stars = max(1, min(5, int(stars)))
    col_ratings().update_one(
        {"_id": _rate_key(user_id, gallery_id)},
        {"$set": {
            "user_id": int(user_id),
            "gallery_id": gallery_id,
            "stars": stars,
            "updated_at": _dt.datetime.utcnow(),
        }},
        upsert=True,
    )


def clear_rating(user_id: int, gallery_id: Any) -> None:
    col_ratings().delete_one({"_id": _rate_key(user_id, gallery_id)})


def get_user_rating(user_id: int, gallery_id: Any) -> Optional[int]:
    d = col_ratings().find_one({"_id": _rate_key(user_id, gallery_id)}, {"stars": 1})
    return int(d["stars"]) if d else None


def get_aggregate_rating(gallery_id: Any) -> dict:
    """Return {avg: float, count: int, dist: {"1":n,"2":n,...}} for a gallery."""
    pipeline = [
        {"$match": {"gallery_id": gallery_id}},
        {"$group": {
            "_id": None,
            "avg":   {"$avg": "$stars"},
            "count": {"$sum": 1},
        }},
    ]
    out = list(col_ratings().aggregate(pipeline))
    if not out:
        return {"avg": 0.0, "count": 0, "dist": {}}
    dist = {str(s): col_ratings().count_documents(
        {"gallery_id": gallery_id, "stars": s}) for s in range(1, 6)}
    return {
        "avg":   round(float(out[0].get("avg") or 0.0), 2),
        "count": int(out[0].get("count") or 0),
        "dist":  dist,
    }


# ---- v11.7 Trending tags -------------------------------------------------
def trending_tags(limit: int = 12, days: int = 7) -> list[dict]:
    """Return the top tags across all bookmarks created in the last `days`.
    Each row: {name, type, count}. Empty list if there are no recent saves."""
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=int(max(1, days)))
    pipeline = [
        {"$match": {"created_at": {"$gte": cutoff}}},
        {"$unwind": "$tags"},
        {"$match": {"tags.name": {"$type": "string", "$ne": ""}}},
        {"$group": {
            "_id":   {"name": "$tags.name", "type": "$tags.type"},
            "count": {"$sum": 1},
        }},
        {"$sort":  {"count": -1}},
        {"$limit": int(max(1, min(50, limit)))},
    ]
    try:
        rows = list(col_bookmarks().aggregate(pipeline))
    except Exception:
        return []
    return [{
        "name":  r["_id"].get("name"),
        "type":  r["_id"].get("type") or "tag",
        "count": int(r.get("count") or 0),
    } for r in rows if r["_id"].get("name")]


def top_user_tags(user_id: int, limit: int = 3) -> list[str]:
    """Return the user's own most-saved tag NAMES (used by tag-aware random +
    recommendations). Excludes the language tag which is always 'english' here."""
    pipeline = [
        {"$match": {"user_id": int(user_id)}},
        {"$unwind": "$tags"},
        {"$match": {
            "tags.name": {"$type": "string", "$ne": ""},
            "tags.type": {"$nin": ["language", "category"]},
        }},
        {"$group":  {"_id": "$tags.name", "count": {"$sum": 1}}},
        {"$sort":   {"count": -1}},
        {"$limit":  int(max(1, min(20, limit)))},
    ]
    try:
        rows = list(col_bookmarks().aggregate(pipeline))
    except Exception:
        return []
    return [str(r["_id"]) for r in rows if r.get("_id")]


def recommend_from_bookmarks(user_id: int, limit: int = 12) -> list[dict]:
    """Collaborative-lite: 'Because you saved X'.
    Find galleries OTHER users bookmarked whose tag overlap with this user's
    top tags is highest, excluding what THIS user already saved.
    Returns [{id, title, cover, pages, tags, score}]."""
    my_tags = set(top_user_tags(user_id, limit=6))
    if not my_tags:
        return []
    my_ids = {b.get("gallery_id") for b in list_bookmarks(user_id, limit=500)}
    # Pull recent bookmarks by others (last 60 days) — cap the scan.
    cutoff = _dt.datetime.utcnow() - _dt.timedelta(days=60)
    cur = (col_bookmarks()
           .find({"user_id": {"$ne": int(user_id)},
                  "created_at": {"$gte": cutoff}},
                 {"gallery_id": 1, "title": 1, "cover": 1, "pages": 1, "tags": 1})
           .sort("created_at", -1)
           .limit(2000))
    scored: dict = {}
    for r in cur:
        gid = r.get("gallery_id")
        if not gid or gid in my_ids:
            continue
        names = {t.get("name") for t in (r.get("tags") or []) if t.get("name")}
        overlap = len(my_tags & names)
        if overlap <= 0:
            continue
        prev = scored.get(gid)
        if prev is None or overlap > prev["score"]:
            scored[gid] = {
                "id":    gid,
                "title": r.get("title"),
                "cover": r.get("cover"),
                "pages": r.get("pages"),
                "tags":  r.get("tags") or [],
                "score": overlap,
            }
    ranked = sorted(scored.values(), key=lambda x: (-x["score"], str(x.get("title") or "")))
    return ranked[: int(max(1, min(50, limit)))]


# ---- v11.7 User stats & badges ------------------------------------------
def user_stats(user_id: int) -> dict:
    """Aggregate counters + earned badges for the profile page."""
    uid = int(user_id)
    saves = col_bookmarks().count_documents({"user_id": uid})
    ratings_given = col_ratings().count_documents({"user_id": uid})
    shares  = col_shares().count_documents({"user_id": uid})
    # Streak: consecutive daily-active days ending today (uses miniapp_usage keys).
    streak = _consecutive_active_days(uid)
    badges = _compute_badges(saves, ratings_given, shares, streak)
    return {
        "saves":         saves,
        "ratings_given": ratings_given,
        "shares":        shares,
        "streak_days":   streak,
        "badges":        badges,
    }


def _consecutive_active_days(user_id: int) -> int:
    keys = {d["_id"] for d in col_usage()
            .find({"_id": {"$regex": f"^{int(user_id)}_"}}, {"_id": 1})}
    if not keys:
        return 0
    today = _dt.date.today()
    streak = 0
    while True:
        k = f"{int(user_id)}_{(today - _dt.timedelta(days=streak)).isoformat()}"
        if k in keys:
            streak += 1
            if streak > 3650:  # 10-year sanity cap
                break
        else:
            break
    return streak


def _compute_badges(saves: int, ratings: int, shares: int, streak: int) -> list[dict]:
    B = []
    def add(icon, name, desc, unlocked):
        B.append({"icon": icon, "name": name, "desc": desc, "unlocked": bool(unlocked)})
    add("🌱", "First Save",   "Bookmark your first gallery",         saves   >= 1)
    add("📚", "Collector",     "Bookmark 10 galleries",                saves   >= 10)
    add("🏰", "Archivist",     "Bookmark 50 galleries",                saves   >= 50)
    add("🏆", "Librarian",     "Bookmark 200 galleries",               saves   >= 200)
    add("⭐", "First Rating", "Rate any gallery",                     ratings >= 1)
    add("🎯", "Critic",        "Rate 25 galleries",                    ratings >= 25)
    add("📤", "Sharer",        "Share your first gallery",             shares  >= 1)
    add("🔥", "3-day Streak",  "Open the app 3 days in a row",         streak  >= 3)
    add("⚡", "Week Streak",  "Open the app 7 days in a row",         streak  >= 7)
    add("💎", "Monthly",       "Open the app 30 days in a row",        streak  >= 30)
    return B


# ---- v11.7 Share events (used by the Sharer badge) ----------------------
def record_share(user_id: int, gallery_id: Any) -> None:
    col_shares().insert_one({
        "user_id":    int(user_id),
        "gallery_id": gallery_id,
        "ts":         _dt.datetime.utcnow(),
    })
