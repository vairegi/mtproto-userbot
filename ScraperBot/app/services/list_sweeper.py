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


async def _priority_pop_n(n: int) -> List[Tuple[str, int]]:
    """Pop up to N entries from the priority queue (used for mid-phase drain)."""
    async with _pending_lock:
        out = list(_pending_pages_for_details[:n])
        del _pending_pages_for_details[:n]
    return out


async def _priority_pop_all_list() -> List[Tuple[str, int]]:
    """Alias for drain_details_hints kept for backward compatibility with
    existing callers in this file that drain the whole queue at phase
    start."""
    return await drain_details_hints()


async def _fetch_and_cache(
    client: httpx.AsyncClient, sort: str, page: int
) -> str:
    """Returns:
        "ok"        — fetched + cached
        "skip"      — bucket exhausted (priority-pushed)
        "rate"      — upstream 429 (priority-pushed)
        "error"     — upstream / write error
    """
    # `sort` may be a raw sort id ('popular') OR a tag pseudo-sort ('tag:incest').
    # Both hit the same cache-key namespace so BOT 0's mini-app reads them via
    # its normal search cache.
    is_tag = sort.startswith("tag:")
    if is_tag:
        tag_name = sort[4:].strip()
        # Upstream: search API with tag query (same as v1.7+)
        query = f"tag:{tag_name}"
        real_sort = "popular"
        # Cache key: BOT 0's user-search format so a user typing the tag
        # name in the mini-app hits this pre-warmed row.
        key = cache.bot0_search_key(tag_name, "popular", page)
    else:
        # Upstream: /galleries with empty query (chip pages).
        query = ""
        real_sort = sort
        # Cache key: BOT 0's chip format — drop-in replacement for
        # prefetch_cron, read by scraper_bridge's home-row path.
        key = cache.bot0_chip_key(sort, page)

    # Dashboard: reflect what we're doing on the activity line.
    from . import channel_dashboard
    channel_dashboard.record_activity(
        sweeping=f"{sort} · page {page}",
        last_tag=(sort[4:].strip() if is_tag else ""),
    )

    if not await cache.try_consume(key):
        log.info("⏭  bucket exhausted key=%s", key)
        _priority_push(sort, page)
        _stats_bump(skips=1)
        channel_dashboard.record_bucket_skip()
        # Update activity so the user sees the skip on Message B.
        channel_dashboard.record_activity(
            sweeping=f"{sort} · page {page} (skipped, retry later)",
        )
        # Longer sleep on skip — keeps us from burning the loop when
        # the bucket is dry (the phase pacing is what protects us, not
        # this sleep, but slowing the skip loop makes Message B readable).
        await asyncio.sleep(settings.list_skip_sleep_sec)
        return "skip"

    try:
        payload = await hf_scraper_lite.fetch_search_page(
            client, query=query, sort=real_sort, page=page,
        )
    except hf_scraper_lite.RateLimited as e:
        log.warning("🚫 429 key=%s retry_after=%s", key, e.retry_after)
        _priority_push(sort, page)
        _stats_bump(rate_limited=1)
        return "rate"
    except hf_scraper_lite.UpstreamError as e:
        log.warning("upstream error key=%s status=%s", key, e.status)
        _stats_bump(errors=1)
        channel_dashboard.record_error()
        return "error"

    write_res = await cache.put(key, payload)
    if not (write_res.get("turso") or write_res.get("mongo")):
        _stats_bump(errors=1)
        channel_dashboard.record_error()
        return "error"

    log.info("📝 WRITE key=%s turso=%s mongo=%s", key,
             write_res.get("turso"), write_res.get("mongo"))
    _stats_bump(writes=1)
    channel_dashboard.record_search_page_written()

    # Hint details_sweeper: freshly-written page's IDs deserve hydration,
    # and pass the sort/tag along so 'new' galleries can be attributed.
    await _register_details_hint(sort, page)
    return "ok"


async def sweep_once() -> Dict[str, Any]:
    """One full sweep across all sorts × pages, plus drain priority queue."""
    if mongo_client.is_paused():
        return {"skipped": "paused"}

    from . import channel_dashboard, trending_tags
    await channel_dashboard.start_phase()

    _stats_reset_current()
    started = time.time()
    ok = skip = rate = err = 0

    # Refresh trending tags from nhentai.net/tags/popular (24h cached).
    trending = []
    try:
        trending = await trending_tags.refresh_if_needed()
    except Exception as e:  # noqa: BLE001
        log.warning("trending_tags refresh failed (non-fatal): %s", e)

    # Build the full sort list = configured core sorts + trending tags +
    # manual EXTRA_TAG_SORTS. Every tag becomes its own sort key, and
    # auto-appears as a `➥ New in "tag: <name>"` line in Message A.
    sorts_this_phase = list(settings.list_sorts)
    for slug in trending:
        entry = f"tag:{slug}"
        if entry not in sorts_this_phase:
            sorts_this_phase.append(entry)
    for t in settings.extra_tag_sorts:
        t = (t or "").strip()
        if t and f"tag:{t}" not in sorts_this_phase:
            sorts_this_phase.append(f"tag:{t}")
    log.info("phase sort plan: %d entries (core=%d, trending=%d, manual=%d)",
             len(sorts_this_phase), len(settings.list_sorts),
             len(trending), len(settings.extra_tag_sorts))

    # v1.8: resume cursor — if Render restarted us mid-phase, continue from
    # the saved (sort_idx, page) instead of restarting from page 1.
    cur = channel_dashboard.cursor_get()
    resume_sort_idx = int(cur.get("sort_idx", 0) or 0)
    resume_page = int(cur.get("page", 1) or 1)

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
        # v1.13: chip sorts sweep list_max_pages (default 30); tag sorts
        # sweep only list_tag_max_pages (default 7). We iterate up to the
        # wider of the two depths and skip a (sort, page) pair when the
        # page exceeds the per-sort cap.
        #
        # v1.14: interleave priority-queue drains every 10 (sort, page)
        # combinations so bucket-skipped pages don't sit waiting for the
        # NEXT phase (up to 6 h away) — they're retried mid-phase once
        # the token bucket has had time to refill.
        chip_max_pages = max(1, int(settings.list_max_pages))
        tag_max_pages  = max(1, int(settings.list_tag_max_pages))
        overall_max    = max(chip_max_pages, tag_max_pages)
        _combos_since_drain = 0
        _DRAIN_EVERY = 10
        _DRAIN_BATCH = 5
        for page in range(1, overall_max + 1):
            for sidx, sort in enumerate(sorts_this_phase):
                # v1.13: enforce per-sort page cap. Tag sorts are prefixed
                # `tag:` in sorts_this_phase; chip sorts are bare
                # ("popular", "date", ...). Skip a tag combo once page > 7.
                is_tag = isinstance(sort, str) and sort.startswith("tag:")
                per_sort_cap = tag_max_pages if is_tag else chip_max_pages
                if page > per_sort_cap:
                    continue
                # Resume: skip combos strictly before the saved cursor.
                if page < resume_page:
                    continue
                if page == resume_page and sidx < resume_sort_idx:
                    continue
                if mongo_client.is_paused():
                    break
                channel_dashboard.cursor_set(sidx, page)
                # Reflect live cursor in the activity fields so the single
                # dashboard message shows the current position.
                channel_dashboard.record_activity(
                    sweeping=f"{sort} · page {page}",
                )
                res = await _fetch_and_cache(client, sort, page)
                if res == "ok":     ok += 1
                elif res == "skip": skip += 1
                elif res == "rate": rate += 1
                else:               err += 1
                await asyncio.sleep(settings.list_delay_sec)

                # v1.14: mid-phase priority drain. Every _DRAIN_EVERY
                # combos, pull up to _DRAIN_BATCH entries from the
                # priority queue and retry them now (bucket has had time
                # to refill since they were skipped). Newly-skipped pages
                # are pushed back to the queue by _fetch_and_cache itself,
                # so we never lose them.
                _combos_since_drain += 1
                if _combos_since_drain >= _DRAIN_EVERY:
                    _combos_since_drain = 0
                    drained = await _priority_pop_n(_DRAIN_BATCH)
                    for (psort, ppage) in drained:
                        if mongo_client.is_paused():
                            _priority_push(psort, ppage)  # put it back
                            continue
                        pres = await _fetch_and_cache(client, psort, ppage)
                        if pres == "ok":     ok += 1
                        elif pres == "skip": skip += 1
                        elif pres == "rate": rate += 1
                        else:                err += 1
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

    # Freeze the dashboard pair with the phase's final numbers.
    try:
        await channel_dashboard.end_phase()
    except Exception as e:  # noqa: BLE001
        log.warning("dashboard end_phase failed: %s", e)

    return {
        "ok": ok, "skip": skip, "rate_limited": rate, "errors": err,
        "duration_sec": round(duration, 2),
    }


async def run_forever(stop_event: asyncio.Event) -> None:
    """Boot task: sweep, then sleep list_tick_sec, forever."""
    log.info("list_sweeper: starting (tick=%ds sorts=%s chip_max_pages=%d tag_max_pages=%d)",
             settings.list_tick_sec, settings.list_sorts,
             settings.list_max_pages, settings.list_tag_max_pages)
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
        "tag_max_pages": settings.list_tag_max_pages,
        "tick_sec":  settings.list_tick_sec,
    }
