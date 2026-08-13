"""
list_sweeper.py — warms search:<sort>:page<N> cache keys.

Loop shape (mirrors BOT 0's prefetch_cron):

  every LIST_TICK_SEC seconds:
      for sort in LIST_SORTS:
          for page in 1..LIST_MAX_PAGES:
              if paused: break
              if not bucket.try_consume("search"): skip, priority-push
              fetch /search?sort=<>&page=<>
              cache.put(search:<sort>:page<N>)
              notify details_sweeper about (sort, page)
              sleep LIST_DELAY_SEC
      round-robin next sort so we don't starve later sorts on 429 storms

State (in Mongo `scraper1_state`):
  * list_last_run           — unix ts of most recent successful sweep
  * list_priority           — [[sort,page], ...] retry queue (skipped pages)
  * list_stats              — {sweeps, writes, skips, errors, last_key}
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Tuple

import httpx

from .. import cache, mongo_client, hf_scraper_lite
from ..config import settings

log = logging.getLogger("scraperbot.list_sweeper")

_PRIO_KEY = "list_priority"
_STATS_KEY = "list_stats"
_LAST_KEY = "list_last_run"
_PRIO_CAP = 40

_pending_pages_for_details: List[Tuple[str, int]] = []
_pending_lock = asyncio.Lock()


def _stats_read() -> Dict[str, Any]:
    return mongo_client.state_get(_STATS_KEY, {}) or {}


def _stats_bump(**delta: int) -> None:
    s = _stats_read()
    for k, v in delta.items():
        s[k] = int(s.get(k, 0)) + int(v)
    mongo_client.state_set(_STATS_KEY, s)


def _stats_reset_current() -> None:
    s = _stats_read()
    s["last_sweep_started"] = time.time()
    mongo_client.state_set(_STATS_KEY, s)


def _priority_push(sort: str, page: int) -> None:
    lst = mongo_client.state_get(_PRIO_KEY, []) or []
    if not isinstance(lst, list):
        lst = []
    entry = [str(sort), int(page)]
    if entry in lst:
        return
    if len(lst) >= _PRIO_CAP:
        lst = lst[-(_PRIO_CAP - 1):]
    lst.append(entry)
    mongo_client.state_set(_PRIO_KEY, lst)


def _priority_pop_all() -> List[Tuple[str, int]]:
    lst = mongo_client.state_get(_PRIO_KEY, []) or []
    if not isinstance(lst, list) or not lst:
        return []
    mongo_client.state_set(_PRIO_KEY, [])
    out: List[Tuple[str, int]] = []
    for entry in lst:
        try:
            out.append((str(entry[0]), int(entry[1])))
        except (IndexError, TypeError, ValueError):
            continue
    return out


async def _register_details_hint(sort: str, page: int) -> None:
    async with _pending_lock:
        _pending_pages_for_details.append((sort, page))
        # Keep this list bounded — details_sweeper drains it every tick.
        if len(_pending_pages_for_details) > 200:
            del _pending_pages_for_details[:100]


async def drain_details_hints() -> List[Tuple[str, int]]:
    """Called by details_sweeper each tick to pick up (sort, page) pairs
    the list_sweeper just wrote — those pages' galleries jump the queue."""
    async with _pending_lock:
        out = list(_pending_pages_for_details)
        _pending_pages_for_details.clear()
    return out


async def _fetch_and_cache(
    client: httpx.AsyncClient, sort: str, page: int
) -> str:
    """Returns:
        "ok"        — fetched + cached
        "skip"      — bucket exhausted (priority-pushed)
        "rate"      — upstream 429 (priority-pushed)
        "error"     — upstream / write error
    """
    key = cache.search_key("", sort, page)

    if not cache.try_consume(key):
        log.info("⏭  bucket exhausted key=%s", key)
        _priority_push(sort, page)
        _stats_bump(skips=1)
        return "skip"

    try:
        payload = await hf_scraper_lite.fetch_search_page(
            client, query="", sort=sort, page=page,
        )
    except hf_scraper_lite.RateLimited as e:
        log.warning("🚫 429 key=%s retry_after=%s", key, e.retry_after)
        _priority_push(sort, page)
        _stats_bump(rate_limited=1)
        return "rate"
    except hf_scraper_lite.UpstreamError as e:
        log.warning("upstream error key=%s status=%s", key, e.status)
        _stats_bump(errors=1)
        return "error"

    write_res = await cache.put(key, payload)
    if not (write_res.get("turso") or write_res.get("mongo")):
        _stats_bump(errors=1)
        return "error"

    log.info("📝 WRITE key=%s turso=%s mongo=%s", key,
             write_res.get("turso"), write_res.get("mongo"))
    _stats_bump(writes=1)

    # Hint details_sweeper: freshly-written page's IDs deserve hydration.
    await _register_details_hint(sort, page)
    return "ok"


async def sweep_once() -> Dict[str, Any]:
    """One full sweep across all sorts × pages, plus drain priority queue."""
    if mongo_client.is_paused():
        return {"skipped": "paused"}

    _stats_reset_current()
    started = time.time()
    ok = skip = rate = err = 0

    client = await hf_scraper_lite.make_client()
    try:
        # Priority queue first — pages that failed last tick.
        for (sort, page) in _priority_pop_all():
            if mongo_client.is_paused():
                break
            res = await _fetch_and_cache(client, sort, page)
            if res == "ok":     ok += 1
            elif res == "skip": skip += 1
            elif res == "rate": rate += 1
            else:               err += 1
            await asyncio.sleep(settings.list_delay_sec)

        # Regular sweep — column-major so no sort starves.
        max_pages = max(1, int(settings.list_max_pages))
        for page in range(1, max_pages + 1):
            for sort in settings.list_sorts:
                if mongo_client.is_paused():
                    break
                res = await _fetch_and_cache(client, sort, page)
                if res == "ok":     ok += 1
                elif res == "skip": skip += 1
                elif res == "rate": rate += 1
                else:               err += 1
                await asyncio.sleep(settings.list_delay_sec)
    finally:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            pass

    duration = time.time() - started
    mongo_client.state_set(_LAST_KEY, time.time())
    _stats_bump(sweeps=1)
    log.info("list sweep done ok=%d skip=%d rate=%d err=%d dur=%.1fs",
             ok, skip, rate, err, duration)
    return {
        "ok": ok, "skip": skip, "rate_limited": rate, "errors": err,
        "duration_sec": round(duration, 2),
    }


async def run_forever(stop_event: asyncio.Event) -> None:
    """Boot task: sweep, then sleep list_tick_sec, forever."""
    log.info("list_sweeper: starting (tick=%ds sorts=%s max_pages=%d)",
             settings.list_tick_sec, settings.list_sorts, settings.list_max_pages)
    while not stop_event.is_set():
        if not settings.scraper_enabled:
            log.info("scraper disabled via SCRAPER_ENABLED=0 — idling")
        else:
            try:
                await sweep_once()
            except Exception as e:  # noqa: BLE001
                log.exception("list_sweeper: unhandled exception: %s", e)
                _stats_bump(errors=1)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.list_tick_sec)
        except asyncio.TimeoutError:
            pass
    log.info("list_sweeper: stopped")


def status() -> Dict[str, Any]:
    return {
        "last_run":  mongo_client.state_get(_LAST_KEY, 0),
        "stats":     _stats_read(),
        "priority":  mongo_client.state_get(_PRIO_KEY, []) or [],
        "paused":    mongo_client.is_paused(),
        "enabled":   settings.scraper_enabled,
        "sorts":     settings.list_sorts,
        "max_pages": settings.list_max_pages,
        "tick_sec":  settings.list_tick_sec,
    }
