"""
cache.py — cache-key helpers + write path.

Key conventions MATCH BOT 0's nhentai_cache.py EXACTLY:
  * gallery detail:     gallery:<id>          TTL 30d
  * search list page:   search:<q>|<sort>|<p> TTL  3d  (q empty for discover)
  * trending block:     trending:<kind>       TTL 30min

Write order (Turso first, Mongo second) is the same order BOT 0 reads in,
so cache reads by BOT 0 return the freshest bytes.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Optional

from . import mongo_client, turso_client
from .config import settings

log = logging.getLogger("scraperbot.cache")


# ---- key builders (byte-for-byte match with BOT 0) -----------------------
def gallery_key(gid: str | int) -> str:
    return f"gallery:{gid}"


def search_key(query: str, sort: str, page: int) -> str:
    q = (query or "").strip().lower()
    s = (sort or "popular").strip().lower()
    p = int(page or 1)
    if len(q) <= 40 and all(c.isalnum() or c in " -_" for c in q):
        return f"search:{q}|{s}|{p}"
    h = hashlib.sha1(q.encode("utf-8")).hexdigest()[:16]
    return f"search:{h}|{s}|{p}"


def trending_key(kind: str = "popular") -> str:
    return f"trending:{kind}"


def ttl_for_key(key: str) -> int:
    if key.startswith("gallery:"):
        return settings.ttl_gallery_sec
    if key.startswith("trending:"):
        return settings.ttl_trending_sec
    return settings.ttl_search_sec


def bucket_for_key(key: str) -> str:
    if key.startswith("gallery:"):
        return "galleries"
    if key.startswith("search:"):
        return "search"
    if key.startswith("trending:"):
        return "popular"
    return "galleries_list"


def bucket_capacity(bucket: str) -> int:
    if bucket == "search":
        return settings.bucket_search
    if bucket == "galleries":
        return settings.bucket_galleries
    if bucket == "popular":
        return 8
    return 15


# ---- write path ----------------------------------------------------------
async def put(key: str, payload: Any) -> dict:
    """Write to Turso first, mirror to Mongo. Both are best-effort."""
    ttl = ttl_for_key(key)
    turso_ok = await turso_client.put(key, payload, ttl)
    mongo_ok = mongo_client.cache_put_mongo(key, payload, ttl)
    return {"turso": bool(turso_ok), "mongo": bool(mongo_ok), "ttl": ttl}


def try_consume(key: str) -> bool:
    """Consume one token for the bucket this key belongs to."""
    b = bucket_for_key(key)
    cap = bucket_capacity(b)
    return mongo_client.bucket_try_consume(b, cap)
