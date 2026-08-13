"""
trending_tags.py — derive "trending tags" from nhentai's JSON galleries API.

Why this implementation
-----------------------
The HTML page /tags/popular is Cloudflare-blocked from Render datacenter
IPs (HTTP 403), and /api/v2/tags does not exist (HTTP 404 — verified).
The reliable JSON source that DOES work from Render is:

    GET /api/v2/galleries?sort=popular-today&page=N   -> HTTP 200

Each gallery in that response carries a full `tags` array
({id, type, name, count, url}). "Trending tags" = the tags that appear
most frequently across the most recent popular-today pages. That is
literally what "trending" means and it uses the exact endpoint the rest
of the bot already relies on.

We pull the first `TRENDING_TAGS_SOURCE_PAGES` (default 3) popular-today
pages, tally tag frequencies, and take the top `TRENDING_TAGS_TOP_N`
slugs. Cached in Mongo under `scraper1_state` for
`TRENDING_TAGS_REFRESH_SEC` (default 24h) so the cost is 3 API calls/day.

Fail-open: any fetch/parse error returns the stale cache (or [] if cold).
The sweeper always keeps manual EXTRA_TAG_SORTS on top of these.
"""
from __future__ import annotations

import logging
import time
from collections import Counter
from typing import List

import httpx

from .. import mongo_client
from ..config import settings

log = logging.getLogger("scraperbot.trending_tags")

_API = "https://nhentai.net/api/v2"
_K_TAGS = "trending_tags"
_K_TS = "trending_tags_fetched_at"

_SOURCE_PAGES = 3      # popular-today pages to harvest ids from
_DETAIL_SAMPLE = 6     # galleries per page to open for tag names
_LIST_DELAY = 1.2      # be gentle with upstream


async def _fetch_gallery_names(c: httpx.AsyncClient, gid: int,
                               headers: dict, counts: Counter) -> None:
    """One /galleries/<id> call — the DETAIL response carries full tag
    dicts [{id, type, name, ...}]. We tally names of type=='tag'."""
    try:
        r = await c.get(f"{_API}/galleries/{gid}", headers=headers)
    except httpx.HTTPError:
        return
    if r.status_code != 200:
        return
    try:
        d = r.json()
    except Exception:
        return
    for tag in (d.get("tags") or []):
        if (isinstance(tag, dict)
                and str(tag.get("type", "")).lower() == "tag"):
            name = str(tag.get("name") or "").strip().lower()
            if name:
                counts[name] += 1


def _now() -> float:
    return time.time()


def cached() -> List[str]:
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
    """Harvest top tags from popular-today galleries when cache is stale."""
    if not settings.trending_tags_enabled:
        return []
    if not is_stale():
        return cached()

    ua = settings.user_agent or (
        "DoujinshiUniverse-ScraperBot/1.7 "
        "(+https://github.com/vairegi/mtproto-userbot)"
    )
    headers = {
        "User-Agent": ua,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://nhentai.net/",
    }

    counts: Counter[str] = Counter()
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            ids: list[int] = []
            for page in range(1, _SOURCE_PAGES + 1):
                try:
                    r = await c.get(
                        f"{_API}/galleries",
                        params={"sort": "popular-today", "page": page},
                        headers=headers,
                    )
                except httpx.HTTPError as e:
                    log.warning("trending_tags page %d transport: %s", page, e)
                    continue
                if r.status_code != 200:
                    log.warning("trending_tags page %d HTTP %s",
                                page, r.status_code)
                    continue
                try:
                    data = r.json()
                except Exception:
                    log.warning("trending_tags page %d non-JSON", page)
                    continue
                # List rows only carry numeric tag_ids — names need the
                # detail endpoint, so collect gallery ids and sample details.
                for gal in (data.get("result") or []):
                    if isinstance(gal, dict) and gal.get("id") is not None:
                        try:
                            ids.append(int(gal["id"]))
                        except (TypeError, ValueError):
                            continue
                import asyncio as _a
                await _a.sleep(_LIST_DELAY)

            sample = ids[: _SOURCE_PAGES * _DETAIL_SAMPLE]
            log.info("trending_tags: sampling %d gallery details for tags",
                     len(sample))
            for gid in sample:
                await _fetch_gallery_names(c, gid, headers, counts)
                import asyncio as _a
                await _a.sleep(_LIST_DELAY)
    except Exception as e:  # noqa: BLE001
        log.warning("trending_tags fetch outer error: %s", e)
        return cached()

    if not counts:
        log.warning("trending_tags: zero tags harvested — keeping cache")
        return cached()

    top = [slug for slug, _ in counts.most_common(
        max(1, int(settings.trending_tags_top_n)))]
    mongo_client.state_set(_K_TAGS, top)
    mongo_client.state_set(_K_TS, _now())
    log.info("trending_tags: refreshed top=%d tags=%s", len(top), top)
    return top
