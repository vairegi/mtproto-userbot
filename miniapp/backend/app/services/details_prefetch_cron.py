"""
details_prefetch_cron.py — v12.11 (#1): background detail scraper

Scrapes gallery DETAILS (id, full title, artist, tags, pages, upload
date, favorites, etc.) for every card on a sort page and uploads them to
Turso via nhentai_cache.put() under the existing `gallery:<id>` key —
the exact same key the mini-app's detail endpoint reads from.

Behavior split (as requested):
  * NIGHT window (00:00–05:00 IST by default): aggressive — runs at
    NIGHT_TICK_SEC cadence with NIGHT_REST_SEC between pages.
  * DAY window  (05:00–24:00 IST by default): cautious — runs only when
    there are NO non-admin users active in the last ACTIVE_WINDOW_SEC
    seconds. Admin activity NEVER pauses the sweep (admin drives it).

Search-bucket cost:
  * A card page is already cached under `search:<sort>:page<N>` by the
    search path (or the main prefetch_cron). We READ from that cache
    instead of re-fetching — so no extra search-bucket tokens.
  * Each card's DETAIL is a single /api/v2/galleries/<id> call,
    consumed via the existing nhentai_cache token bucket (`galleries`
    bucket) so user traffic always wins.

Admin surface:
  * DEDUP-style fail-open env knobs, admin-panel Enable/Disable via the
    standard control_flags store (same one /popupon uses), live status
    dict consumed by the admin route.

Env knobs:
  DETAILS_SCRAPER_ENABLED            "1"/"0" master switch (default 1)
  DETAILS_SCRAPER_NIGHT_START_IST    int 0-23 (default 0)
  DETAILS_SCRAPER_NIGHT_END_IST      int 0-23 (default 5)
  DETAILS_SCRAPER_NIGHT_TICK_SEC     int seconds between pages in night window (default 300)
  DETAILS_SCRAPER_DAY_TICK_SEC       int seconds between pages in day window  (default 60)
  DETAILS_SCRAPER_NIGHT_REST_SEC     int sleep between gallery fetches in night (default 2)
  DETAILS_SCRAPER_DAY_REST_SEC       int sleep between gallery fetches in day   (default 5)
  DETAILS_SCRAPER_ACTIVE_WINDOW_SEC  int seconds of "recently active" for user pause (default 300)
  DETAILS_SCRAPER_PAGE_CAP           int max page index per sort (default 20)
  DETAILS_SCRAPER_MAX_PAGES_PER_TICK int how many (sort,page) tuples per tick (default 1)
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("miniapp.details_prefetch_cron")


# ---------------------------------------------------------------------------
# Env helpers (mirrors prefetch_cron).
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


ENABLED:        bool = _env_bool("DETAILS_SCRAPER_ENABLED",            True)
NIGHT_START:    int  = _env_int ("DETAILS_SCRAPER_NIGHT_START_IST",    0)
NIGHT_END:      int  = _env_int ("DETAILS_SCRAPER_NIGHT_END_IST",      5)
NIGHT_TICK_SEC: int  = _env_int ("DETAILS_SCRAPER_NIGHT_TICK_SEC",     300)
DAY_TICK_SEC:   int  = _env_int ("DETAILS_SCRAPER_DAY_TICK_SEC",       60)
NIGHT_REST_SEC: int  = _env_int ("DETAILS_SCRAPER_NIGHT_REST_SEC",     2)
DAY_REST_SEC:   int  = _env_int ("DETAILS_SCRAPER_DAY_REST_SEC",       5)
ACTIVE_WINDOW:  int  = _env_int ("DETAILS_SCRAPER_ACTIVE_WINDOW_SEC",  300)
PAGE_CAP:       int  = _env_int ("DETAILS_SCRAPER_PAGE_CAP",           20)
PAGES_PER_TICK: int  = _env_int ("DETAILS_SCRAPER_MAX_PAGES_PER_TICK", 1)

# Sorts we walk, in priority order. "date" is the mini-app's New Uploads.
_SORTS: List[str] = ["popular-today", "date", "popular-week", "popular"]

# v12.11 (#1b): search-time opportunistic hydration. When the search path
# serves a page, scraper_bridge calls notify_page() and the (sort, page)
# tuple lands here. The next scrape tick drains these FIRST (a user is
# literally looking at this page RIGHT NOW), then falls back to the
# round-robin walk. A set is enough — dedup is free and order within a
# tick doesn't matter much.
_priority_pages: set = set()


def notify_page(sort: str, page: int) -> None:
    """Called (best-effort) by scraper_bridge when a search page is served
    so its cards' details get hydrated on the very next tick instead of
    waiting for the round-robin walk to reach them. Never raises."""
    try:
        if sort in _SORTS and 1 <= int(page) <= PAGE_CAP:
            _priority_pages.add((sort, int(page)))
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Cross-run state — read by /api/admin/details-scraper (admin.js panel).
# ---------------------------------------------------------------------------
_state: Dict[str, Any] = {
    "enabled":            bool(ENABLED),
    "started_at":         None,   # epoch of the current run (or None)
    "finished_at":        None,
    "current_sort":       None,
    "current_page":       None,
    "current_gallery_id": None,
    "galleries_done_this_run": 0,
    "galleries_skipped_this_run": 0,
    "galleries_failed_this_run": 0,
    "run_count":          0,
    "last_error":         None,
    "paused_reason":      None,   # 'night' / 'day-active-users' / None
    "phase":              "idle", # idle / running / paused
    "turso_error":        None,   # RULE 7.5 disclosure
}


def last_run_summary() -> Dict[str, Any]:
    snap = dict(_state)
    snap["now"] = int(time.time())
    snap["config"] = {
        "night_start_ist": NIGHT_START,
        "night_end_ist":   NIGHT_END,
        "night_tick_sec":  NIGHT_TICK_SEC,
        "day_tick_sec":    DAY_TICK_SEC,
        "night_rest_sec":  NIGHT_REST_SEC,
        "day_rest_sec":    DAY_REST_SEC,
        "active_window":   ACTIVE_WINDOW,
        "page_cap":        PAGE_CAP,
        "pages_per_tick":  PAGES_PER_TICK,
        "sorts":           list(_SORTS),
    }
    return snap


# ---------------------------------------------------------------------------
# Time-of-day + user-activity predicates.
# ---------------------------------------------------------------------------
def _ist_hour() -> int:
    """Current hour in IST (UTC+5:30), 0-23."""
    now_utc = datetime.datetime.utcnow()
    ist = now_utc + datetime.timedelta(hours=5, minutes=30)
    return ist.hour


def _is_night_window() -> bool:
    """True inside the [NIGHT_START, NIGHT_END) IST window."""
    h = _ist_hour()
    if NIGHT_START <= NIGHT_END:
        return NIGHT_START <= h < NIGHT_END
    # Wraps midnight, e.g. 22-05.
    return h >= NIGHT_START or h < NIGHT_END


def _has_active_non_admin_users() -> bool:
    """True when any non-admin user was seen within ACTIVE_WINDOW seconds."""
    try:
        from .. import db as _db  # miniapp db layer (has list_users)
        import config as _bot_cfg  # bot-side config for admin_user_id
    except Exception:  # noqa: BLE001
        # Fail-safe: if we can't read user state, pretend users are active
        # so we never silently abuse the API during the day window.
        return True

    cutoff = time.time() - ACTIVE_WINDOW
    try:
        users = _db.list_users(limit=200)
    except Exception:  # noqa: BLE001
        return True

    admin_uid = None
    try:
        admin_uid = int(getattr(_bot_cfg.settings, "admin_user_id", 0) or 0)
    except Exception:  # noqa: BLE001
        admin_uid = None

    for u in users:
        ls = u.get("last_seen")
        if ls is None:
            continue
        # last_seen is a datetime; normalize to epoch.
        try:
            if isinstance(ls, datetime.datetime):
                ls_epoch = ls.replace(tzinfo=datetime.timezone.utc).timestamp()
            else:
                ls_epoch = float(ls)
        except Exception:  # noqa: BLE001
            continue
        if ls_epoch < cutoff:
            continue
        # Active non-admin user → pause. Admins never pause the sweep.
        uid = u.get("_id")
        if admin_uid is not None and int(uid) == admin_uid:
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# Cache-layer + scrape-layer access (lazy imports, same pattern as prefetch).
# ---------------------------------------------------------------------------
_cache_mod = None
_scraper_bridge_mod = None


def _get_cache():
    global _cache_mod
    if _cache_mod is not None:
        return _cache_mod
    try:
        from . import nhentai_cache as _nc  # noqa: WPS433
        _cache_mod = _nc
    except Exception as e:  # noqa: BLE001
        log.warning("details_scraper: nhentai_cache import failed (%s)", e)
        _cache_mod = None
    return _cache_mod


def _get_scraper():
    global _scraper_bridge_mod
    if _scraper_bridge_mod is not None:
        return _scraper_bridge_mod
    try:
        from . import scraper_bridge as _sb  # noqa: WPS433
        _scraper_bridge_mod = _sb
    except Exception as e:  # noqa: BLE001
        log.warning("details_scraper: scraper_bridge import failed (%s)", e)
        _scraper_bridge_mod = None
    return _scraper_bridge_mod


def _search_cache_key(sort: str, page: int) -> str:
    """Same key format scraper_bridge + prefetch_cron already use."""
    return f"search:{sort}:page{int(page)}"


def _gallery_cache_key(gallery_id: str) -> str:
    """Same key format scraper_bridge._direct_nhentai_detail uses."""
    return f"gallery:{gallery_id}"


# ---------------------------------------------------------------------------
# One tick: walk up to PAGES_PER_TICK (sort,page) tuples, hydrate details
# for every card that doesn't already have a fresh Turso row.
# ---------------------------------------------------------------------------
async def _scrape_page_details(sort: str, page: int) -> Dict[str, int]:
    """Hydrate the detail cache for one (sort, page) tuple. Never raises."""
    cache = _get_cache()
    scraper = _get_scraper()
    out = {"done": 0, "skipped": 0, "failed": 0}

    if cache is None or scraper is None:
        return out

    # Read the search page from the cache. We do NOT re-fetch — that would
    # consume search-bucket tokens. If it's not cached yet we skip this
    # page entirely; the main prefetch_cron will fill it on its own tick.
    skey = _search_cache_key(sort, page)
    try:
        items = cache.get(skey, allow_stale=True)
    except Exception as e:  # noqa: BLE001
        log.debug("details_scraper: cache.get(%s) raised: %s", skey, e)
        items = None

    if not isinstance(items, list) or not items:
        return out

    # Per-card detail loop. Consume the galleries bucket so user traffic
    # always wins; sleep between fetches per the night/day rest value.
    rest = NIGHT_REST_SEC if _is_night_window() else DAY_REST_SEC
    for item in items:
        gid = item.get("id") if isinstance(item, dict) else None
        if gid in (None, ""):
            out["skipped"] += 1
            continue
        gid = str(gid).strip()
        _state["current_gallery_id"] = gid

        gkey = _gallery_cache_key(gid)

        # Skip if already cached fresh — the 30-day TTL on gallery rows
        # means most pages are a no-op on the second pass.
        try:
            if cache.get(gkey, allow_stale=False) is not None:
                out["skipped"] += 1
                continue
        except Exception as e:  # noqa: BLE001
            log.debug("details_scraper: cache.get(%s) probe raised: %s", gkey, e)

        # Token-bucket guard — never starve users.
        try:
            allowed = bool(cache.try_consume("galleries", cost=1.0))
        except Exception as e:  # noqa: BLE001
            log.debug("details_scraper: try_consume raised: %s", e)
            allowed = True
        if not allowed:
            out["skipped"] += 1
            await asyncio.sleep(rest)
            continue

        # Fetch the detail via the SAME sync wrapper the route uses so a
        # successful fetch lands in Turso under the exact same key.
        try:
            detail = scraper._direct_nhentai_detail(gid)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            detail = None
            _state["last_error"] = f"detail fetch {gid}: {e}"[:200]
            log.warning("details_scraper: detail fetch %s failed: %s", gid, e)

        if isinstance(detail, dict) and detail.get("id"):
            out["done"] += 1
        else:
            out["failed"] += 1

        # Polite rest between gallery fetches (user-requested).
        await asyncio.sleep(rest)

    return out


async def scrape_once() -> Dict[str, Any]:
    """Run one tick. Never raises. Returns last_run_summary() at the end."""
    if not ENABLED:
        _state["enabled"] = False
        _state["phase"] = "idle"
        return last_run_summary()

    _state["enabled"] = True
    _state["started_at"] = int(time.time())
    _state["finished_at"] = None
    _state["run_count"] += 1
    _state["galleries_done_this_run"] = 0
    _state["galleries_skipped_this_run"] = 0
    _state["galleries_failed_this_run"] = 0
    _state["last_error"] = None
    _state["turso_error"] = None

    # Decide whether to run now.
    if not _is_night_window():
        if _has_active_non_admin_users():
            _state["phase"] = "paused"
            _state["paused_reason"] = "day-active-users"
            _state["finished_at"] = int(time.time())
            return last_run_summary()
    else:
        # Night window — user pause does NOT apply (they're asleep).
        pass

    _state["phase"] = "running"
    _state["paused_reason"] = None

    cache = _get_cache()
    if cache is None:
        _state["turso_error"] = "nhentai_cache module unavailable"
        _state["phase"] = "paused"
        _state["paused_reason"] = "cache-module-unavailable"
        _state["finished_at"] = int(time.time())
        return last_run_summary()

    # v12.11 (#1b): drain user-visible pages FIRST. A page the user just
    # opened is hydrated before any round-robin work this tick.
    walked = 0
    while _priority_pages and walked < PAGES_PER_TICK:
        sort, page = _priority_pages.pop()
        _state["current_sort"] = sort
        _state["current_page"] = page
        try:
            res = await _scrape_page_details(sort, page)
        except Exception as e:  # noqa: BLE001
            res = {"done": 0, "skipped": 0, "failed": 0}
            _state["last_error"] = f"priority page {sort}:{page} raised: {e}"[:200]
            log.exception("details_scraper: priority page %s:%s crashed: %s", sort, page, e)
        _state["galleries_done_this_run"]    += int(res.get("done", 0))
        _state["galleries_skipped_this_run"] += int(res.get("skipped", 0))
        _state["galleries_failed_this_run"]  += int(res.get("failed", 0))
        walked += 1

    # Walk the sort × page grid in priority order, one page per tick.
    for sort in _SORTS:
        if walked >= PAGES_PER_TICK:
            break
        for page in range(1, PAGE_CAP + 1):
            if walked >= PAGES_PER_TICK:
                break
            _state["current_sort"] = sort
            _state["current_page"] = page
            try:
                res = await _scrape_page_details(sort, page)
            except Exception as e:  # noqa: BLE001
                res = {"done": 0, "skipped": 0, "failed": 0}
                _state["last_error"] = f"page {sort}:{page} raised: {e}"[:200]
                log.exception("details_scraper: page %s:%s crashed: %s", sort, page, e)
            _state["galleries_done_this_run"]    += int(res.get("done", 0))
            _state["galleries_skipped_this_run"] += int(res.get("skipped", 0))
            _state["galleries_failed_this_run"]  += int(res.get("failed", 0))
            walked += 1

    _state["finished_at"] = int(time.time())
    _state["phase"] = "idle"
    log.info(
        "details_scraper: tick end sort=%s page=%s done=%d skipped=%d failed=%d",
        _state["current_sort"], _state["current_page"],
        _state["galleries_done_this_run"],
        _state["galleries_skipped_this_run"],
        _state["galleries_failed_this_run"],
    )
    return last_run_summary()


async def run_forever() -> None:
    """Sleep / tick / sleep loop. Same fail-open contract as prefetch_cron."""
    log.info(
        "details_scraper: run_forever start night=%s-%s IST day_tick=%ss night_tick=%ss",
        NIGHT_START, NIGHT_END, DAY_TICK_SEC, NIGHT_TICK_SEC,
    )
    while True:
        try:
            if ENABLED:
                await scrape_once()
            else:
                log.debug("details_scraper: disabled by env — idle tick")
        except asyncio.CancelledError:
            log.info("details_scraper: run_forever cancelled — stopping")
            raise
        except Exception as e:  # noqa: BLE001
            _state["last_error"] = f"tick crashed: {e!s}"[:200]
            log.exception("details_scraper: tick crashed (continuing): %s", e)

        tick = NIGHT_TICK_SEC if _is_night_window() else DAY_TICK_SEC
        try:
            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            log.info("details_scraper: run_forever cancelled during sleep — stopping")
            raise
