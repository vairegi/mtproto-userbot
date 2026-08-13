"""
details_sweeper.py — warms gallery:<id> cache keys.

Round-robin walks the (sort, page) grid, reads the already-cached search
page from Turso/Mongo (zero search-bucket cost — the list_sweeper wrote
it), extracts gallery IDs, and fetches per-gallery detail via
/api/v2/galleries/<id> for any that aren't cached yet or whose entry is
expired.

Priority queue: (sort, page) tuples that list_sweeper JUST wrote get
hydrated on the very next tick.

State (Mongo `scraper1_state`):
  * details_cursor        — {sort_idx, page}  round-robin position
  * details_last_run      — unix ts
  * details_stats         — {sweeps, writes, skips, errors, hits}
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from .. import cache, mongo_client, turso_client, hf_scraper_lite
from ..config import settings
from . import list_sweeper

log = logging.getLogger("scraperbot.details_sweeper")

_CURSOR_KEY = "details_cursor"
_STATS_KEY  = "details_stats"
_LAST_KEY   = "details_last_run"


def _stats_read() -> Dict[str, Any]:
    return mongo_client.state_get(_STATS_KEY, {}) or {}


def _stats_bump(**delta: int) -> None:
    s = _stats_read()
    for k, v in delta.items():
        s[k] = int(s.get(k, 0)) + int(v)
    mongo_client.state_set(_STATS_KEY, s)


def _cursor_read() -> Tuple[int, int]:
    c = mongo_client.state_get(_CURSOR_KEY, {}) or {}
    return int(c.get("sort_idx", 0) or 0), int(c.get("page", 1) or 1)


def _cursor_write(sort_idx: int, page: int) -> None:
    mongo_client.state_set(_CURSOR_KEY, {
        "sort_idx": int(sort_idx),
        "page": int(page),
        "updated_at": time.time(),
    })


def _cursor_advance() -> Tuple[str, int]:
    """Advance round-robin. Returns the (sort, page) tuple to work on NEXT
    (before the advance) so callers get a stable current pointer."""
    sorts = settings.list_sorts or ["popular"]
    sort_idx, page = _cursor_read()
    sort_idx = sort_idx % len(sorts)
    current_sort = sorts[sort_idx]
    current_page = page

    # Advance for next call: page++, wrap into next sort at cap.
    next_page = current_page + 1
    next_sort_idx = sort_idx
    if next_page > int(settings.details_page_cap or 20):
        next_page = 1
        next_sort_idx = (sort_idx + 1) % len(sorts)
    _cursor_write(next_sort_idx, next_page)
    return current_sort, current_page


async def _read_search_cache(sort: str, page: int) -> Optional[Dict[str, Any]]:
    """Read a search-page blob straight from the shared cache. Never fetches."""
    key = cache.search_key("", sort, page)
    hit = await turso_client.get(key)
    if hit and hit.get("payload"):
        return hit["payload"]
    m = mongo_client.cache_get_mongo(key)
    if m and m.get("payload"):
        return m["payload"]
    return None


async def _gallery_is_fresh(gid: str) -> bool:
    """True if gallery:<id> is present AND not expired in either backend."""
    key = cache.gallery_key(gid)
    now = time.time()
    hit = await turso_client.get(key)
    if hit and int(hit.get("expires_at", 0)) > now:
        return True
    m = mongo_client.cache_get_mongo(key)
    if m and float(m.get("expires_at", 0)) > now:
        return True
    return False


async def _fetch_one_gallery(
    client: httpx.AsyncClient, gid: str
) -> str:
    """One /galleries/<id> call → cache. Returns ok / hit / skip / rate / error."""
    key = cache.gallery_key(gid)

    if await _gallery_is_fresh(gid):
        _stats_bump(hits=1)
        return "hit"

    if not cache.try_consume(key):
        log.info("⏭  galleries bucket exhausted gid=%s", gid)
        _stats_bump(skips=1)
        return "skip"

    try:
        payload = await hf_scraper_lite.fetch_gallery(client, gid)
    except hf_scraper_lite.RateLimited as e:
        log.warning("🚫 429 gid=%s retry_after=%s", gid, e.retry_after)
        _stats_bump(rate_limited=1)
        return "rate"
    except hf_scraper_lite.UpstreamError as e:
        log.warning("upstream error gid=%s status=%s", gid, e.status)
        _stats_bump(errors=1)
        return "error"

    write_res = await cache.put(key, payload)
    if not (write_res.get("turso") or write_res.get("mongo")):
        _stats_bump(errors=1)
        return "error"

    log.info("📝 detail WRITE gid=%s turso=%s mongo=%s", gid,
             write_res.get("turso"), write_res.get("mongo"))
    _stats_bump(writes=1)
    return "ok"


async def _work_page(
    client: httpx.AsyncClient, sort: str, page: int
) -> Dict[str, int]:
    """Hydrate every gallery ID visible on (sort, page). Reads the cached
    list page — no /search call is made."""
    tally = {"ok": 0, "hit": 0, "skip": 0, "rate": 0, "error": 0, "no_ids": 0}
    payload = await _read_search_cache(sort, page)
    if not payload:
        # list_sweeper hasn't written this page yet — skip cheaply.
        tally["no_ids"] = 1
        return tally
    ids = hf_scraper_lite.extract_ids_from_search(payload)
    if not ids:
        tally["no_ids"] = 1
        return tally

    per_tick = max(1, int(settings.details_per_tick or 5))
    worked = 0
    for gid in ids:
        if worked >= per_tick:
            break
        if mongo_client.is_paused():
            break
        res = await _fetch_one_gallery(client, gid)
        tally[res if res in tally else "error"] += 1
        # Only sleep if we actually made an upstream call.
        if res in ("ok", "rate", "error"):
            worked += 1
            await asyncio.sleep(settings.details_rest_sec)
    return tally


async def sweep_once() -> Dict[str, Any]:
    """One tick: hydrate hint pages first, then one round-robin page."""
    if mongo_client.is_paused():
        return {"skipped": "paused"}

    started = time.time()
    combined = {"ok": 0, "hit": 0, "skip": 0, "rate": 0, "error": 0, "no_ids": 0}

    client = await hf_scraper_lite.make_client()
    try:
        # 1) Priority: freshly-written list pages from list_sweeper.
        hints = await list_sweeper.drain_details_hints()
        for (sort, page) in hints[:4]:  # cap per tick
            if mongo_client.is_paused():
                break
            t = await _work_page(client, sort, page)
            for k, v in t.items():
                combined[k] = combined.get(k, 0) + v

        # 2) Round-robin: one (sort, page) advance per tick.
        if not mongo_client.is_paused():
            sort, page = _cursor_advance()
            t = await _work_page(client, sort, page)
            for k, v in t.items():
                combined[k] = combined.get(k, 0) + v
            combined["cursor"] = f"{sort}#{page}"
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass

    duration = time.time() - started
    mongo_client.state_set(_LAST_KEY, time.time())
    _stats_bump(sweeps=1)
    log.info("details sweep done %s dur=%.1fs", combined, duration)
    combined["duration_sec"] = round(duration, 2)
    return combined


async def run_forever(stop_event: asyncio.Event) -> None:
    log.info("details_sweeper: starting (tick=%ds per_tick=%d page_cap=%d)",
             settings.details_tick_sec, settings.details_per_tick,
             settings.details_page_cap)
    # Gentle start-up: give list_sweeper a head start so cache exists.
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=15)
        return
    except asyncio.TimeoutError:
        pass
    while not stop_event.is_set():
        if not settings.scraper_enabled:
            log.info("scraper disabled via SCRAPER_ENABLED=0 — idling")
        else:
            try:
                await sweep_once()
            except Exception as e:  # noqa: BLE001
                log.exception("details_sweeper: unhandled: %s", e)
                _stats_bump(errors=1)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.details_tick_sec)
        except asyncio.TimeoutError:
            pass
    log.info("details_sweeper: stopped")


def status() -> Dict[str, Any]:
    sort_idx, page = _cursor_read()
    sorts = settings.list_sorts or ["popular"]
    return {
        "last_run": mongo_client.state_get(_LAST_KEY, 0),
        "stats":    _stats_read(),
        "cursor":   {"sort": sorts[sort_idx % len(sorts)], "page": page},
        "paused":   mongo_client.is_paused(),
        "enabled":  settings.scraper_enabled,
        "tick_sec": settings.details_tick_sec,
        "per_tick": settings.details_per_tick,
        "page_cap": settings.details_page_cap,
    }
