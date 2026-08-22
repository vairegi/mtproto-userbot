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

# v1.20: Mongo cache mirror is rollback-only, OFF by default.
_TURSO_ONLY = os.environ.get("BOT1_CACHE_MONGO_MIRROR", "0").strip() not in ("0", "", "false", "no")


# ---- key builders (byte-for-byte match with BOT 0) -----------------------
def gallery_key(gid: str | int) -> str:
    return f"gallery:{gid}"


def search_key(query: str, sort: str, page: int) -> str:
    """Legacy BOT 1 format — kept for backward compat, DO NOT use for new writes."""
    q = (query or "").strip().lower()
    s = (sort or "popular").strip().lower()
    p = int(page or 1)
    if len(q) <= 40 and all(c.isalnum() or c in " -_" for c in q):
        return f"search:{q}|{s}|{p}"
    h = hashlib.sha1(q.encode("utf-8")).hexdigest()[:16]
    return f"search:{h}|{s}|{p}"


def bot0_chip_key(sort: str, page: int) -> str:
    """BOT 0's empty-query chip format — what prefetch_cron writes and
    scraper_bridge reads for home-page rows.
    Matches: search:popular-today:page1
    """
    return f"search:{(sort or 'popular').strip().lower()}:page{int(page or 1)}"


def bot0_search_key(query: str, sort: str, page: int) -> str:
    """BOT 0's user-typed query format — what scraper_bridge reads when a
    user types into the search box.
    Matches: search:q=sole female|sort=popular|page=1

    v1.11: query normalization made byte-identical to BOT 0's
    hf_scraper.search() (v12.19): lowercase + internal whitespace
    collapse ("  Sole   Female " -> "sole female"). Without the collapse,
    a multi-space query produced different keys on the two bots and the
    warm row was never hit.
    """
    q = " ".join((query or "").lower().split())
    s = (sort or "popular").strip().lower()
    return f"search:q={q}|sort={s}|page={int(page or 1)}"


def bucket_for_key(key: str) -> str:
    # Route new key formats to the right nhentai bucket. Chip + q= keys
    # both hit /api/v2/search — same bucket.
    if key.startswith("gallery:"):    return "galleries"
    if key.startswith("search:"):     return "search"
    if key.startswith("suggest:"):    return "suggestions"
    if key.startswith("trending:"):   return "popular"
    return "galleries_list"


def trending_key(kind: str = "popular") -> str:
    return f"trending:{kind}"


def ttl_for_key(key: str) -> int:
    if key.startswith("gallery:"):
        return settings.ttl_gallery_sec
    if key.startswith("trending:"):
        return settings.ttl_trending_sec
    return settings.ttl_search_sec


# v1.12: never-expire predicate. Chip-sort and tag-sort search pages are
# fully owned by this bot's continuous sweep — every phase INSERT-OR-REPLACEs
# the whole page, so new items appear and removed items disappear
# automatically. TTL freshness makes no sense for a row we authoritatively
# rewrite; instead we stamp expires_at=0 on write and BOT 0's read path
# (nhentai_cache.py v12.20) treats that as "always fresh, never call
# nhentai".
import os as _os_ne
NEVER_EXPIRE_CHIP_TAG = _os_ne.environ.get(
    "NHCACHE_CHIP_TAG_NEVER_EXPIRE", "1").strip() not in ("0", "false", "False", "")


def is_chip_or_tag_key(key: str) -> bool:
    """Byte-identical predicate to BOT 0's _is_chip_or_tag_key. Two formats:
        search:<sort>:page<N>              chip sorts (bot0_chip_key)
        search:q=<q>|sort=<s>|page=<N>     tag / typed-search sorts (bot0_search_key)
    """
    if not isinstance(key, str) or not key.startswith("search:"):
        return False
    tail = key[len("search:"):]
    if "|" not in tail and tail.count(":") == 1 and ":page" in tail:
        return True
    if tail.startswith("q=") and "|sort=" in tail and "|page=" in tail:
        return True
    return False


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
        # v1.14: BOT 1 deliberately uses a LOWER effective capacity for the
        # /search bucket than BOT 0's documented 10/min. Both bots now share
        # the same Turso nhentai_ratelimit row, so the TOTAL across both bots
        # must stay under nhentai's anon limit. BOT 0 (user-facing) gets the
        # full 10/min; BOT 1 (background) caps itself at bucket_search_scraper
        # (default 8) so user requests always have headroom.
        return settings.bucket_search_scraper
    if bucket == "galleries":
        return settings.bucket_galleries
    if bucket == "popular":
        return 8
    return 15


# ---- write path ----------------------------------------------------------
async def put(key: str, payload: Any) -> dict:
    """Write to Turso first, mirror to Mongo. Both are best-effort.

    v1.12: chip and tag keys pass ttl_sec=0 to signal never-expire. Both
    writers (turso_client.put, mongo_client.cache_put_mongo) treat ttl==0
    as "stamp expires_at=0 sentinel" — BOT 0's read path recognises the
    sentinel and always serves the row without falling back to nhentai.
    """
    if NEVER_EXPIRE_CHIP_TAG and is_chip_or_tag_key(key):
        ttl = 0
    else:
        ttl = ttl_for_key(key)
    turso_ok = await turso_client.put(key, payload, ttl)
    mongo_ok = False if _TURSO_ONLY else mongo_client.cache_put_mongo(key, payload, ttl)
    return {"turso": bool(turso_ok), "mongo": bool(mongo_ok), "ttl": ttl}


async def try_consume(key: str) -> bool:
    """Consume one token for the bucket this key belongs to.

    v1.14: now async because the Turso-backed bucket path is async. Both
    call sites (list_sweeper._fetch_and_cache, details_sweeper) are async
    and await this. Also honours the v1.14 per-bot search cap: BOT 1's
    /search consumption is clamped to settings.bucket_search_scraper
    (default 8/min) so BOT 0's user-facing requests always win the last
    2 tokens of the shared 10/min /search bucket."""
    b = bucket_for_key(key)
    cap = bucket_capacity(b)
    # v1.19: region-aware bucket split. When BOT1_REGION is set (e.g.
    # "ap-singapore" on the Singapore service), spend from a region-
    # suffixed bucket row so this side never throttles / is throttled by
    # the other-region bot sharing the legacy row. Empty suffix -> legacy
    # bucket id, byte-identical to pre-v1.19 behavior.
    if settings.region_suffix:
        b = f"{b}_{settings.region_suffix}"
    return await mongo_client.bucket_try_consume(b, cap)
