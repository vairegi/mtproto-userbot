"""
trending_tags.py — scrape nhentai.net/tags/popular for the current top-N tags.

Per user preference: use the HTML listing (weekly rotating trending), not the
API's all-time popular sort. Result is cached in Mongo `scraper1_state` under
`trending_tags` with a refresh timestamp so the network hit only happens
once per TRENDING_TAGS_REFRESH_SEC (default 24h).

Fail-open: if the fetch fails, we return whatever's in the cache; if the
cache is empty too, we return an empty list. The sweeper always keeps the
manual EXTRA_TAG_SORTS regardless.
"""
from __future__ import annotations

import logging
import re
import time
from typing import List

import httpx

from .. import mongo_client
from ..config import settings

log = logging.getLogger("scraperbot.trending_tags")

_URL = "https://nhentai.net/tags/popular"
_K_TAGS = "trending_tags"
_K_TS = "trending_tags_fetched_at"

# The tag listing renders each tag as
#   <a href="/tag/big-breasts/" class="tag ...">...
# We grab the tag slug from the href — robust to CSS class churn.
_TAG_RE = re.compile(r'href="/tag/([a-z0-9\-]+)/?"', re.IGNORECASE)


def _now() -> float:
    return time.time()


def cached() -> List[str]:
    """Return the currently-cached trending tag slugs (may be stale)."""
    v = mongo_client.state_get(_K_TAGS, []) or []
    return [str(t) for t in v if isinstance(t, str) and t.strip()][
        : max(1, int(settings.trending_tags_top_n))
    ]


def is_stale() -> bool:
    ts = mongo_client.state_get(_K_TS, 0) or 0
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        ts = 0.0
    return (_now() - ts) > max(300, int(settings.trending_tags_refresh_sec))


async def refresh_if_needed() -> List[str]:
    """Fetch nhentai.net/tags/popular if the cache is stale; return the
    top-N slugs regardless. Uses a browser-y UA so the CDN doesn't 403."""
    if not settings.trending_tags_enabled:
        return []
    if not is_stale():
        return cached()

    ua = settings.user_agent or (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
    headers = {
        "User-Agent": ua,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://nhentai.net/",
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(_URL, headers=headers)
    except httpx.HTTPError as e:
        log.warning("trending_tags fetch transport error: %s", e)
        return cached()
    if r.status_code != 200:
        log.warning("trending_tags fetch HTTP %s", r.status_code)
        return cached()

    seen: list[str] = []
    seen_set: set[str] = set()
    for m in _TAG_RE.finditer(r.text):
        slug = m.group(1).strip().lower()
        if not slug or slug in seen_set:
            continue
        seen_set.add(slug)
        seen.append(slug)

    if not seen:
        log.warning("trending_tags: parsed zero tags from HTML — keeping cache")
        return cached()

    top = seen[: max(1, int(settings.trending_tags_top_n))]
    mongo_client.state_set(_K_TAGS, top)
    mongo_client.state_set(_K_TS, _now())
    log.info("trending_tags: refreshed top=%d tags=%s", len(top), top)
    return top
