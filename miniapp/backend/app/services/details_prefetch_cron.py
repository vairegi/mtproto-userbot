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
# tuple lands in the priority queue. The next scrape tick drains these
# FIRST (a user is literally looking at this page RIGHT NOW), then falls
# back to the round-robin walk.
#
# v12.12 (autoscraper fix): the priority queue is persisted to Mongo, NOT
# held in memory — notify_page() runs in the BACKEND process (it serves
# /api/search) while the cron loop runs in the WORKER process (separate
# Render service). An in-memory set would never cross the process
# boundary. Same reason the enable-flag and status snapshot live in
# Mongo (see _persist_state / _read_enabled below).
_FLAG_KEY = "details_scraper_enabled"
_STATE_KEY = "details_scraper_state"
_PRIO_KEY = "details_scraper_priority"
_PRIO_CAP = 50


def _db_set(key: str, value: Any) -> None:
    try:
        from .. import db as _midb
        _midb.set_setting(key, value)
    except Exception as e:  # noqa: BLE001
        log.debug("details_scraper: db_set(%s) failed: %s", key, e)


def _db_get(key: str, default: Any = None) -> Any:
    try:
        from .. import db as _midb
        return _midb.get_setting(key, default)
    except Exception as e:  # noqa: BLE001
        log.debug("details_scraper: db_get(%s) failed: %s", key, e)
        return default


def notify_page(sort: str, page: int) -> None:
    """Called (best-effort) by scraper_bridge when a search page is served
    so its cards' details get hydrated on the very next tick instead of
    waiting for the round-robin walk to reach them. Never raises."""
    try:
        if sort in _SORTS and 1 <= int(page) <= PAGE_CAP:
            lst = _db_get(_PRIO_KEY, []) or []
            if not isinstance(lst, list):
                lst = []
            entry = [sort, int(page)]
            if entry not in lst:
                lst.append(entry)
            _db_set(_PRIO_KEY, lst[-_PRIO_CAP:])  # cap: keep freshest
    except Exception:  # noqa: BLE001
        pass


def _drain_priority_pages() -> List[Tuple[str, int]]:
    """Pop every queued (sort, page) tuple from the shared Mongo queue."""
    lst = _db_get(_PRIO_KEY, []) or []
    if not isinstance(lst, list) or not lst:
        return []
    _db_set(_PRIO_KEY, [])
    out: List[Tuple[str, int]] = []
    for e in lst:
        try:
            s, p = e[0], int(e[1])
            if s in _SORTS and 1 <= p <= PAGE_CAP:
                out.append((s, p))
        except Exception:  # noqa: BLE001
            continue
    return out


def _read_enabled() -> bool:
    """Runtime toggle: DB flag wins over the env default so the admin
    Enable/Disable button (which lives in the backend process) actually
    reaches this worker process."""
    flag = _db_get(_FLAG_KEY, None)
    return bool(flag) if flag is not None else bool(ENABLED)


# ---------------------------------------------------------------------------
# Cross-run state — read by /api/admin/details-scraper (admin.js panel).
# ---------------------------------------------------------------------------
# v12.13 (#D): per-tick skip-reason counters. Every one of these is a
# concrete, mutually-exclusive reason a card was NOT hydrated in the last
# tick. Zero-initialised each tick so the admin panel shows fresh numbers.
_SKIP_REASONS: Tuple[str, ...] = (
    "already_cached_fresh",     # gallery:<id> is fresh AND schema-complete
    "no_search_page_cached",    # search:<sort>:page<N> not in cache yet
    "missing_gallery_id",       # search page row has no id field
    "token_bucket_denied",      # galleries bucket refused a token
    "upstream_detail_empty",    # scraper returned None/empty dict
    "cache_write_failed",       # detail fetched but nhentai_cache.put failed
)


def _fresh_skip_counters() -> Dict[str, int]:
    return {r: 0 for r in _SKIP_REASONS}


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
    # v12.13 (#D): per-tick skip breakdown — tells the admin WHY a tick
    # showed skipped=25 done=0 instead of forcing them to guess.
    "skip_reasons":       _fresh_skip_counters(),
    # v12.13 (#D): plain-English one-liner surfaced in the admin panel.
    "status_text":        "Idle — waiting for next tick.",
}


# ---------------------------------------------------------------------------
# v12.13 (#D): schema-completeness test.
#
# The old code called cache.get("gallery:<id>") is-not-None the "fresh"
# check. That's true even if the payload is an early v12.10 row that
# only carries {id, title, cover, pages} — no artist / tags / uploaded /
# num_favorites. Those rows LOOK fresh but are useless to the mini-app's
# detail sheet, and the autoscraper would sit at skipped=25 forever
# claiming everything was already cached.
#
# _is_schema_complete() defines the minimum shape a gallery:<id> row must
# have to count as "actually cached". Anything short of that is treated as
# a miss, so the autoscraper will re-fetch and rewrite the full detail.
# ---------------------------------------------------------------------------
_REQUIRED_DETAIL_FIELDS: Tuple[str, ...] = (
    "id", "title", "tags", "num_pages",
)
_RECOMMENDED_DETAIL_FIELDS: Tuple[str, ...] = (
    "artist", "uploaded", "num_favorites", "cover",
)


def _is_schema_complete(detail: Any) -> bool:
    """Return True when a cached gallery:<id> payload has enough fields to
    render the detail sheet without a re-fetch. Missing REQUIRED fields
    fails the check; missing RECOMMENDED fields also fails so the sweep
    upgrades early v12.10 rows over time. Never raises.
    """
    if not isinstance(detail, dict):
        return False
    for f in _REQUIRED_DETAIL_FIELDS:
        if not detail.get(f):
            return False
    for f in _RECOMMENDED_DETAIL_FIELDS:
        # `cover` may legitimately be empty for very old uploads; treat
        # its absence (key not present) as incomplete, but its presence
        # with a falsy value as acceptable to avoid infinite re-fetch.
        if f not in detail:
            return False
    return True


def _persist_state() -> None:
    """v12.12 (autoscraper fix): write the live state snapshot to Mongo so
    the admin panel (backend process) can read what the worker process is
    doing. Cheap — one upsert per tick."""
    try:
        _db_set(_STATE_KEY, dict(_state))
    except Exception:  # noqa: BLE001
        pass


def _plain_english_status(state: Dict[str, Any]) -> str:
    """v12.13 (#D): one-line human-readable status for the admin panel."""
    if not state.get("enabled"):
        return "Paused by admin."
    phase = str(state.get("phase") or "idle")
    reason = state.get("paused_reason")
    if phase == "paused":
        if reason == "day-active-users":
            return "Waiting: users are active, will continue after they leave."
        if reason == "cache-module-unavailable":
            return "Paused: cache module unavailable — check nhentai_cache import."
        return f"Paused ({reason or 'unknown reason'})."
    sort = state.get("current_sort")
    page = state.get("current_page")
    skips = state.get("skip_reasons") or {}
    done  = int(state.get("galleries_done_this_run") or 0)
    if phase == "running":
        pretty_sort = {
            "popular-today": "Popular Now",
            "popular-week":  "Popular Week",
            "popular":       "Popular",
            "date":          "New Uploads",
        }.get(sort, sort or "?")
        return f"Working: checking {pretty_sort}, page {page or '?'}."
    if done > 0:
        return f"Idle — last tick saved {done} new gallery detail(s)."
    if int(skips.get("no_search_page_cached", 0)) > 0:
        return "Waiting for search page cache to arrive."
    if int(skips.get("token_bucket_denied", 0)) > 0:
        return "Waiting: upstream token bucket is empty, will retry next tick."
    if int(skips.get("already_cached_fresh", 0)) > 0:
        n = int(skips.get("already_cached_fresh", 0))
        return f"Nothing needed: all {n} galleries already have details saved."
    if int(skips.get("missing_gallery_id", 0)) > 0:
        return "Skipped: some search rows had no gallery id."
    if int(skips.get("upstream_detail_empty", 0)) > 0:
        return "Some detail fetches returned empty — will retry next tick."
    return "Idle — waiting for next tick."


def last_run_summary() -> Dict[str, Any]:
    # v12.12: prefer the persisted snapshot — in the backend process the
    # in-memory _state is empty (the cron lives in the worker process).
    persisted = _db_get(_STATE_KEY, None)
    snap = dict(persisted) if isinstance(persisted, dict) else dict(_state)
    if not snap:
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
    snap["enabled"] = _read_enabled()
    # v12.13 (#D): guarantee the skip_reasons dict + plain-English line
    # are ALWAYS present in the response.
    if not isinstance(snap.get("skip_reasons"), dict):
        snap["skip_reasons"] = _fresh_skip_counters()
    else:
        for r in _SKIP_REASONS:
            snap["skip_reasons"].setdefault(r, 0)
    snap["status_text"] = _plain_english_status(snap)
    snap["explainer"] = {
        "night_day": (
            f"NIGHT window is {NIGHT_START:02d}:00–{NIGHT_END:02d}:00 IST. "
            f"During NIGHT the scraper works every {NIGHT_TICK_SEC}s regardless of "
            f"user activity. During DAY it only works when no non-admin users have "
            f"been active in the last {ACTIVE_WINDOW}s, tick every {DAY_TICK_SEC}s."
        ),
        "skipped": (
            "'skipped' counts galleries the tick chose NOT to fetch. Broken "
            "down under skip_reasons: already_cached_fresh (nothing to do), "
            "no_search_page_cached (waiting for search cache), missing_gallery_id "
            "(bad row), token_bucket_denied (users have priority), "
            "upstream_detail_empty (fetch returned nothing), cache_write_failed "
            "(Turso/Mongo rejected the write)."
        ),
        "next_action": (
            f"Next check: {sorted(list(_SORTS))} — one page per tick, up to "
            f"page {PAGE_CAP} per sort, then it loops back."
        ),
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
        # v12.13 (#D): search page not cached yet — record a distinct reason.
        skips = _state.get("skip_reasons") or _fresh_skip_counters()
        skips["no_search_page_cached"] = int(skips.get("no_search_page_cached", 0)) + 1
        _state["skip_reasons"] = skips
        out["skipped"] += 1
        return out

    # Per-card detail loop. Consume the galleries bucket so user traffic
    # always wins; sleep between fetches per the night/day rest value.
    rest = NIGHT_REST_SEC if _is_night_window() else DAY_REST_SEC
    skips = _state.get("skip_reasons") or _fresh_skip_counters()
    for item in items:
        gid = item.get("id") if isinstance(item, dict) else None
        if gid in (None, ""):
            skips["missing_gallery_id"] = int(skips.get("missing_gallery_id", 0)) + 1
            out["skipped"] += 1
            continue
        gid = str(gid).strip()
        _state["current_gallery_id"] = gid

        gkey = _gallery_cache_key(gid)

        # v12.13 (#D): schema-completeness test. Old rows that only carry
        # {id, title, cover, pages} used to satisfy the is-not-None probe
        # and get skipped forever. Now we treat an incomplete row as a
        # miss and let the fetch below upgrade it in place.
        try:
            existing = cache.get(gkey, allow_stale=False)
        except Exception as e:  # noqa: BLE001
            log.debug("details_scraper: cache.get(%s) probe raised: %s", gkey, e)
            existing = None
        if existing is not None and _is_schema_complete(existing):
            skips["already_cached_fresh"] = int(skips.get("already_cached_fresh", 0)) + 1
            out["skipped"] += 1
            continue

        # Token-bucket guard — never starve users.
        try:
            allowed = bool(cache.try_consume("galleries", cost=1.0))
        except Exception as e:  # noqa: BLE001
            log.debug("details_scraper: try_consume raised: %s", e)
            allowed = True
        if not allowed:
            skips["token_bucket_denied"] = int(skips.get("token_bucket_denied", 0)) + 1
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
            # v12.13 (#D): confirm schema-complete AND that the cache write
            # actually landed (paranoid readback).
            if not _is_schema_complete(detail):
                skips["upstream_detail_empty"] = int(skips.get("upstream_detail_empty", 0)) + 1
                out["skipped"] += 1
            else:
                try:
                    readback = cache.get(gkey, allow_stale=False)
                except Exception:  # noqa: BLE001
                    readback = None
                if readback is None:
                    skips["cache_write_failed"] = int(skips.get("cache_write_failed", 0)) + 1
                    out["failed"] += 1
                else:
                    out["done"] += 1
        else:
            skips["upstream_detail_empty"] = int(skips.get("upstream_detail_empty", 0)) + 1
            out["failed"] += 1

        # Polite rest between gallery fetches (user-requested).
        await asyncio.sleep(rest)

    _state["skip_reasons"] = skips
    return out


async def scrape_once() -> Dict[str, Any]:
    """Run one tick. Never raises. Returns last_run_summary() at the end."""
    # v12.12: read the toggle from Mongo every tick so the admin panel's
    # Enable/Disable (backend process) actually reaches this worker.
    if not _read_enabled():
        _state["enabled"] = False
        _state["phase"] = "idle"
        _persist_state()
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
    # v12.13 (#D): zero the per-tick skip breakdown.
    _state["skip_reasons"] = _fresh_skip_counters()

    # Decide whether to run now.
    if not _is_night_window():
        if _has_active_non_admin_users():
            _state["phase"] = "paused"
            _state["paused_reason"] = "day-active-users"
            _state["finished_at"] = int(time.time())
            _persist_state()
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
        _persist_state()
        return last_run_summary()

    # v12.12: drain user-visible pages FIRST from the SHARED Mongo queue
    # (the in-memory set never crossed the backend→worker process boundary).
    walked = 0
    for sort, page in _drain_priority_pages():
        if walked >= PAGES_PER_TICK:
            break
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
    _persist_state()
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
            # v12.12: DB-backed toggle so the admin button reaches us.
            if _read_enabled():
                await scrape_once()
            else:
                log.debug("details_scraper: disabled — idle tick")
                _state["enabled"] = False
                _state["phase"] = "idle"
                _persist_state()
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
