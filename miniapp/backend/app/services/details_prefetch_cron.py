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
PAGE_CAP:       int  = _env_int ("DETAILS_SCRAPER_PAGE_CAP",           30)   # v12.15: raised 20 -> 30 per user request
PAGES_PER_TICK: int  = _env_int ("DETAILS_SCRAPER_MAX_PAGES_PER_TICK", 1)
# v12.15: Phase 2 tag-search depth (user picked "first 5 pages per tag").
TAG_PAGE_CAP:   int  = _env_int ("DETAILS_SCRAPER_TAG_PAGE_CAP",       5)
# v12.15: how many (sort,page) pairs to advance per tick when the current
# pair is already fully cached. Each advance costs one cache probe; 8 is
# a sane upper bound that lets the walker sprint past fully-cached pages
# without hogging the worker.
SKIP_FAST_CAP:  int  = _env_int ("DETAILS_SCRAPER_SKIP_FAST_CAP",      8)
# v12.15: hard-coded popular tags the user asked for. Phase 2 sweeps these
# first, then auto-extends from nhentai's trending-tags list when the
# hard-coded queue is exhausted (hybrid mode).
DEFAULT_TAGS: Tuple[str, ...] = (
    "incest", "mother", "sister", "milf",
    "big-breasts", "schoolgirl", "ahegao", "anal",
)

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
# v12.15: durable sweep position so a Render restart doesn't reset the
# walker back to (popular-today, page 1). This is the single source of
# truth for where the breadth-first sweep currently is.
_SWEEP_KEY = "details_scraper_sweep"
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


# ---------------------------------------------------------------------------
# v12.15: BREADTH-FIRST SWEEP STATE MACHINE
# ---------------------------------------------------------------------------
# What the user actually wants (paraphrased from the v12.14 feedback):
#
#   Phase 1 — Sorts sweep (BREADTH-FIRST, not depth-first)
#     Tick N:   popular-today page 1
#     Tick N+1: date (New Uploads) page 1
#     Tick N+2: popular-week page 1
#     Tick N+3: popular page 1
#     Tick N+4: popular-today page 2
#     Tick N+5: date page 2
#     ... and so on until every sort has been walked through page 30
#     (i.e. 4 sorts × 30 pages = 120 pairs per sweep).
#
#   Phase 2 — Tag sweep (only after Phase 1 is complete)
#     For each tag in the hard-coded DEFAULT_TAGS list, then any tags
#     nhentai's trending-tags endpoint surfaces (hybrid mode), scrape the
#     first 5 pages of search results for that tag and hydrate every card.
#
#   On full-sweep completion (Phase 1 + Phase 2 both done):
#     Send a Telegram alert to the admin ONCE, then loop back to Phase 1
#     and start a fresh sweep (per user's choice — nhentai uploads new
#     content daily).
#
# Persistence contract:
#   The sweep position lives in Mongo under _SWEEP_KEY. It MUST be durable
#   across Render restarts — otherwise the walker resets to (popular-today,
#   page 1) on every recycle and never advances, which is exactly the bug
#   the user reported in the v12.14 screenshot.
#
# Skip-fast advance:
#   When the walker lands on a page whose 25 cards are ALL already schema-
#   complete, it advances to the NEXT pair in the SAME tick instead of
#   wasting the whole tick. Cap: SKIP_FAST_CAP advances per tick so the
#   worker still has head-room for real user traffic.
# ---------------------------------------------------------------------------


def _default_sweep_state() -> Dict[str, Any]:
    """Fresh sweep position. Page index is 1-based; sort_idx/tag_idx are
    0-based indexes into _SORTS / the active tag list."""
    return {
        "phase":            1,            # 1 = sorts sweep, 2 = tag sweep
        "sort_idx":         0,
        "page":             1,
        "tag_idx":          0,
        "tag_page":         1,
        "tags_done":        [],           # tags fully swept in this pass
        "tags_active":      list(DEFAULT_TAGS),
        "alert_sent":       False,        # fire-once flag for the admin alert
        "sweeps_completed": 0,            # how many full sweeps we've done
        "last_advanced_at": None,
    }


def _load_sweep() -> Dict[str, Any]:
    """Read the durable sweep position from Mongo; fall back to a fresh
    state on any error. Never raises — a corrupt sweep doc just means we
    start the sweep over, which is safe (the schema gate makes pages
    idempotent)."""
    try:
        raw = _db_get(_SWEEP_KEY, None)
    except Exception as e:  # noqa: BLE001
        log.debug("🔍🐞 autoscraper: sweep load failed: %s", e)
        raw = None
    if not isinstance(raw, dict):
        return _default_sweep_state()
    base = _default_sweep_state()
    for k, v in raw.items():
        if k in base:
            base[k] = v
    return base


def _save_sweep(sw: Dict[str, Any]) -> None:
    """Persist the sweep position to Mongo. Never raises."""
    try:
        sw["last_advanced_at"] = int(time.time())
        _db_set(_SWEEP_KEY, dict(sw))
    except Exception as e:  # noqa: BLE001
        log.debug("🔍🐞 autoscraper: sweep save failed: %s", e)


def _next_sort_pair(sw: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """Breadth-first (sort, page) walker for Phase 1.

    Returns the CURRENT (sort, page) tuple, then mutates `sw` to point at
    the NEXT one. Returns None when every sort × page combination has
    been visited (the caller should transition to Phase 2).

    Order: page 1 of every sort, then page 2 of every sort, ... up to
    PAGE_CAP. This is what the user explicitly asked for — not the old
    depth-first walk that finished all 30 pages of popular-today before
    touching 'date'.
    """
    if sw["phase"] != 1:
        return None
    # v12.15 fix (was a real bug): the bounds check must happen BEFORE we
    # return the pair. The old code returned (sort, page) even after page
    # had already advanced past PAGE_CAP, so Phase 1 never ended and the
    # walker kept re-serving pages past the cap. New order: roll sort_idx
    # over to 0 and bump page when sort_idx wraps, THEN check page cap,
    # THEN return the pair.
    if sw["sort_idx"] >= len(_SORTS):
        sw["sort_idx"] = 0
        sw["page"] += 1
    if sw["page"] > PAGE_CAP:
        return None
    sort = _SORTS[sw["sort_idx"]]
    page = sw["page"]
    # Advance: sort_idx walks INNER (fastest), page walks OUTER (slowest).
    # That's what makes it breadth-first across sorts.
    sw["sort_idx"] += 1
    return (sort, page)


def _phase1_complete(sw: Dict[str, Any]) -> bool:
    """True when every sort has been walked through PAGE_CAP pages."""
    return sw["phase"] == 1 and sw["page"] > PAGE_CAP


def _fetch_trending_tags(scraper) -> List[str]:
    """Auto-extend the tag list from nhentai's trending-tags endpoint.
    Returns an empty list on any failure (we never let this break the
    sweep — the hard-coded DEFAULT_TAGS still cover the common cases)."""
    try:
        import httpx
        from . import scraper_bridge as _sb  # noqa: WPS433
        r = httpx.get(
            "https://nhentai.net/api/v2/tags/popular",
            headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        payload = r.json() or {}
        tags = payload.get("tags") if isinstance(payload, dict) else None
        if not isinstance(tags, list):
            return []
        out = []
        for t in tags:
            if isinstance(t, dict) and t.get("name"):
                out.append(str(t["name"]).strip().lower().replace(" ", "-"))
        return [t for t in out if t and t not in DEFAULT_TAGS]
    except Exception as e:  # noqa: BLE001
        log.warning("🔍⚠️ autoscraper: trending-tags fetch failed: %s", e)
        return []


def _next_tag_pair(sw: Dict[str, Any], scraper) -> Optional[Tuple[str, int]]:
    """Breadth-first (tag, page) walker for Phase 2.

    Returns the CURRENT (tag, page) tuple, then mutates `sw` to point at
    the NEXT one. Returns None when every tag has been walked through
    TAG_PAGE_CAP pages (the caller should send the admin alert and loop
    back to Phase 1).

    Hybrid mode: when the hard-coded tags run out, we extend the active
    tag list from nhentai's trending-tags endpoint ONCE per sweep.
    """
    if sw["phase"] != 2:
        return None
    # Auto-extend tags if the hard-coded list is exhausted and we haven't
    # yet pulled trending tags this sweep.
    if sw["tag_idx"] >= len(sw["tags_active"]):
        new_tags = _fetch_trending_tags(scraper)
        if new_tags:
            sw["tags_active"] = list(sw["tags_active"]) + new_tags
            log.info("🔍🏷️ autoscraper: extended tag list with %d trending tags", len(new_tags))
        else:
            return None  # truly nothing left to sweep
    if sw["tag_idx"] >= len(sw["tags_active"]):
        return None
    tag = sw["tags_active"][sw["tag_idx"]]
    page = sw["tag_page"]
    # Advance: page walks INNER (fastest), tag walks OUTER (slowest).
    # We fully sweep one tag's 5 pages before moving to the next tag.
    sw["tag_page"] += 1
    if sw["tag_page"] > TAG_PAGE_CAP:
        sw["tags_done"] = list(sw.get("tags_done", [])) + [tag]
        sw["tag_idx"] += 1
        sw["tag_page"] = 1
    return (tag, page)


def _phase2_complete(sw: Dict[str, Any]) -> bool:
    """True when every active tag has been walked through TAG_PAGE_CAP pages."""
    if sw["phase"] != 2:
        return False
    return sw["tag_idx"] >= len(sw["tags_active"])


async def _send_admin_alert(text: str) -> bool:
    """Fire-once Telegram alert to the admin. Same transport as the dedup
    cron's alert — best-effort, never raises."""
    try:
        import os
        import httpx
        bot_token = os.getenv("ADMIN_BOT_TOKEN") or os.getenv("BOT_TOKEN") or ""
        admin_chat = os.getenv("ADMIN_CHAT_ID") or os.getenv("ADMIN_USER_ID") or ""
        if not (bot_token and admin_chat):
            log.warning("🔍📣 autoscraper: alert skipped — ADMIN_BOT_TOKEN or ADMIN_CHAT_ID missing")
            return False
        r = httpx.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={"chat_id": admin_chat, "text": text},
            timeout=10,
        )
        if r.status_code == 200:
            log.info("🔍📣 autoscraper: admin alert sent")
            return True
        log.warning("🔍⚠️ autoscraper: alert HTTP %s: %s", r.status_code, r.text[:200])
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("🔍⚠️ autoscraper: alert raised: %s", e)
        return False


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
    # v12.15: durable sweep position mirrored into _state so the admin
    # panel can show it. The authoritative copy lives in Mongo under
    # _SWEEP_KEY; these fields are just the last-loaded snapshot.
    "sweep_phase":            1,     # 1 = sorts sweep, 2 = tag sweep
    "sweep_sort_idx":         0,
    "sweep_page":             1,
    "sweep_tag_idx":          0,
    "sweep_tag_page":         1,
    "sweep_sweeps_completed": 0,
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
# v12.14 ROOT-CAUSE FIX for the 3–4 hour "upstream_detail_empty=25" outage:
# the v12.13 gate above was checking `num_pages`, `num_favorites`,
# `uploaded`, `artist` — field names that scraper_bridge does NOT emit.
# The real dict scraper_bridge._direct_nhentai_detail() returns has:
#   id / title / cover / pages / favorites / upload_date / tags /
#   tag_groups / scanlator / title_english / title_japanese / page1_url
# Every fetched detail therefore failed the v12.13 gate and was
# reclassified as `upstream_detail_empty`, so the panel showed
# skipped=25 done=0 forever even though HTTP fetches were succeeding.
#
# The new gate matches the ACTUAL emitted shape. tag_groups.artist is
# accepted as sufficient artist evidence — that's what the detail
# sheet reads for the artist row anyway.
_REQUIRED_DETAIL_FIELDS: Tuple[str, ...] = (
    "id", "title", "tags", "pages", "cover",
)


def _is_schema_complete(detail: Any) -> bool:
    """Return True when a cached gallery:<id> payload has enough fields to
    render the detail sheet without a re-fetch. Never raises.

    v12.14: field names now match scraper_bridge._direct_nhentai_detail's
    real output. `pages` (not num_pages), `favorites` (not num_favorites),
    `upload_date` (not uploaded), and `tag_groups` for artist — that's
    what the scraper actually writes to Turso.
    """
    if not isinstance(detail, dict):
        return False
    for f in _REQUIRED_DETAIL_FIELDS:
        if detail.get(f) in (None, "", 0):
            return False
    # tag_groups must exist as a dict (may be empty for very sparse
    # uploads — empty is acceptable, missing is not).
    if not isinstance(detail.get("tag_groups"), dict):
        return False
    # upload_date / favorites may legitimately be 0 or "" on old uploads;
    # require the KEY to be present so we know the fetch actually ran.
    if "upload_date" not in detail or "favorites" not in detail:
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
    """v12.15: one-line status in truly simple English, emoji-prefixed.
    Now includes the current sweep phase and position."""
    if not state.get("enabled"):
        return "⏸️ Turned off by admin."
    phase = str(state.get("phase") or "idle")
    reason = state.get("paused_reason")
    if phase == "paused":
        if reason == "day-active-users":
            return "⏳ Waiting because real users are using the app right now."
        if reason == "cache-module-unavailable":
            return "⚠️ Paused: cache module is missing — check server logs."
        return f"⏸️ Paused ({reason or 'unknown reason'})."
    sort = state.get("current_sort")
    page = state.get("current_page")
    skips = state.get("skip_reasons") or {}
    done  = int(state.get("galleries_done_this_run") or 0)
    sweep_phase = int(state.get("sweep_phase") or 1)
    sweep_page  = int(state.get("sweep_page") or 1)
    sweep_tag_idx = int(state.get("sweep_tag_idx") or 0)
    sweep_tag_page = int(state.get("sweep_tag_page") or 1)
    sweeps_done = int(state.get("sweep_sweeps_completed") or 0)

    # v12.15: if the sweep is currently in Phase 2, surface the tag sweep.
    if sweep_phase == 2:
        tag = (state.get("current_sort") or "").replace("tag:", "") or "—"
        return (
            f"🏷️ Phase 2 (tag sweep): working on tag '{tag}' page {sweep_tag_page} — "
            f"this is tag #{sweep_tag_idx + 1} of the popular-tag list."
        )

    if phase == "running":
        pretty_sort = {
            "popular-today": "Popular Now",
            "popular-week":  "Popular Week",
            "popular":       "Popular",
            "date":          "New Uploads",
        }.get(sort, sort or "?")
        return f"🔍 Working on {pretty_sort}, page {page or '?'} (Phase 1 — sorts sweep)."
    if done > 0:
        return f"✅ Just saved {done} new gallery detail(s). Idle until next tick."
    if int(skips.get("no_search_page_cached", 0)) > 0:
        return "⏳ Waiting: the list page it needs is not cached yet."
    if int(skips.get("token_bucket_denied", 0)) > 0:
        return "🚦 Waiting: nhentai has no tokens left — real users get priority."
    if int(skips.get("already_cached_fresh", 0)) > 0:
        n = int(skips.get("already_cached_fresh", 0))
        return (
            f"💾 All {n} galleries on this page are already saved — "
            f"the scraper will jump to the next page on its next tick."
        )
    if int(skips.get("missing_gallery_id", 0)) > 0:
        return "⚠️ Some search rows had no gallery id — they were skipped."
    if int(skips.get("upstream_detail_empty", 0)) > 0:
        return "⚠️ nhentai returned empty details — will retry next tick."
    if sweeps_done > 0:
        return f"💤 Idle — sweep #{sweeps_done} complete, waiting for next tick."
    return "💤 Idle — waiting for next tick."


def _friendly_explainer_lines(state: Dict[str, Any]) -> Dict[str, str]:
    """v12.15: super-simple sentence per row of the admin panel, now with
    Phase 1 / Phase 2 awareness so the admin can tell at a glance which
    part of the sweep the scraper is in."""
    phase   = str(state.get("phase") or "idle")
    paused  = state.get("paused_reason")
    sort    = state.get("current_sort") or "—"
    page    = state.get("current_page") or "—"
    gid     = state.get("current_gallery_id") or "—"
    done    = int(state.get("galleries_done_this_run") or 0)
    skipped = int(state.get("galleries_skipped_this_run") or 0)
    sweep_phase  = int(state.get("sweep_phase") or 1)
    sweep_page   = int(state.get("sweep_page") or 1)
    sweep_tag_idx = int(state.get("sweep_tag_idx") or 0)
    sweep_tag_page = int(state.get("sweep_tag_page") or 1)
    sweeps_done  = int(state.get("sweep_sweeps_completed") or 0)
    failed  = int(state.get("galleries_failed_this_run") or 0)
    runs    = int(state.get("run_count") or 0)
    return {
        "phase":    ("phase = idle — it means open to work, waiting for the next tick"
                     if phase == "idle"
                     else "phase = running — it means currently scraping RIGHT NOW"
                     if phase == "running"
                     else f"phase = {phase} — paused, will resume automatically"),
        "paused":   ("paused = no — it means the scraper is actively working"
                     if not paused
                     else f"paused = {paused} — waiting because " + str(paused).replace("-", " ")),
        "current":  f"current = {sort} page {page} · gallery {gid} — the scraper is on the "
                    f"'{sort}' list, page {page}, looking at gallery id {gid}",
        "this_run": f"this run = done {done} · skipped {skipped} · failed {failed} — "
                    f"how many gallery details this tick saved / skipped / failed",
        "run_count": f"run count = {runs} — how many times the scraper has woken up since "
                     f"the process started",
        "skip_help": "skip reasons — if 'skipped' is not zero, one of the numbers below "
                     "will match it and tell you WHY",
        "already":   "already_cached_fresh — detail is already saved, no need to fetch again",
        "nosearch":  "no_search_page_cached — the list page (Popular / New / …) isn't "
                     "cached yet, will try later",
        "missingid": "missing_gallery_id — the search row had a broken id, safely skipped",
        "token":     "token_bucket_denied — nhentai's rate-limit is full, real users get "
                     "priority over this scraper",
        "empty":     "upstream_detail_empty — nhentai answered but sent nothing usable, "
                     "will retry (v12.14 fixed the false positive from v12.13)",
        "writefail": "cache_write_failed — detail was fetched but Turso/Mongo refused to "
                     "save it (rare)",
        "window":    "window — NIGHT 12am–5am IST runs every 5 min (fast). DAY runs every "
                     "60s only if no users are using the app",
        "rest":      "rest — seconds to wait between two gallery fetches so nhentai doesn't "
                     "rate-limit us",
        "active":    "active window — a user counts as 'active' if they touched the app in "
                     "the last 5 minutes",
        "page_cap":  f"page cap — Phase 1 walks up to page {PAGE_CAP} of each sort "
                     f"(4 sorts × {PAGE_CAP} pages = {4 * PAGE_CAP} pages total per sweep)",
        # v12.15: sweep-position rows.
        "sweep":     (f"sweep = Phase {sweep_phase} — "
                      + (f"sorts sweep, currently on page {sweep_page} of {PAGE_CAP} "
                         f"(sort #{int(state.get('sweep_sort_idx') or 0) + 1} of 4)"
                         if sweep_phase == 1
                         else f"tag sweep, currently on tag #{sweep_tag_idx + 1} page "
                              f"{sweep_tag_page} of {TAG_PAGE_CAP}")),
        "sweeps":    f"sweeps completed = {sweeps_done} — how many FULL sweeps "
                     f"(Phase 1 + Phase 2) the scraper has finished since the process started",
    }


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
        "tag_page_cap":    TAG_PAGE_CAP,        # v12.15
        "skip_fast_cap":   SKIP_FAST_CAP,       # v12.15
        "pages_per_tick":  PAGES_PER_TICK,
        "sorts":           list(_SORTS),
        "default_tags":    list(DEFAULT_TAGS),  # v12.15
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
    # v12.14: line-by-line simple explanation, one short sentence per row.
    # Frontend renders each key as its own bullet under the technical block.
    snap["explainer_v2"] = _friendly_explainer_lines(snap)
    # v12.13 explainer kept for backward compat with any older frontend build.
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
        # v12.14: emoji-tagged Render log line — easy to grep.
        log.info("🔍⏳ autoscraper: %s page %s not cached yet, will retry next tick", sort, page)
        return out

    # v12.14: log start of per-page scan so admins can follow ticks in Render.
    log.info("🔍📄 autoscraper: scanning %s page %s (%d cards)", sort, page, len(items))

    # Per-card detail loop. Consume the galleries bucket so user traffic
    # always wins; sleep between fetches per the night/day rest value.
    rest = NIGHT_REST_SEC if _is_night_window() else DAY_REST_SEC
    skips = _state.get("skip_reasons") or _fresh_skip_counters()
    for item in items:
        gid = item.get("id") if isinstance(item, dict) else None
        if gid in (None, ""):
            skips["missing_gallery_id"] = int(skips.get("missing_gallery_id", 0)) + 1
            out["skipped"] += 1
            log.warning("🔍⚠️ autoscraper: %s p%s row has no gallery id, skipped", sort, page)
            continue
        gid = str(gid).strip()
        _state["current_gallery_id"] = gid

        gkey = _gallery_cache_key(gid)

        # v12.13 (#D) / v12.14: schema-completeness test now matches the
        # real scraper_bridge dict shape (pages/favorites/upload_date/
        # tag_groups). Old rows that only carry {id, title, cover, pages}
        # used to satisfy the is-not-None probe and get skipped forever.
        try:
            existing = cache.get(gkey, allow_stale=False)
        except Exception as e:  # noqa: BLE001
            log.debug("🔍🐞 autoscraper: cache.get(%s) probe raised: %s", gkey, e)
            existing = None
        if existing is not None and _is_schema_complete(existing):
            skips["already_cached_fresh"] = int(skips.get("already_cached_fresh", 0)) + 1
            out["skipped"] += 1
            log.debug("🔍💾 autoscraper: id=%s already saved and complete, skipping", gid)
            continue

        # Token-bucket guard — never starve users.
        try:
            allowed = bool(cache.try_consume("galleries", cost=1.0))
        except Exception as e:  # noqa: BLE001
            log.debug("🔍🐞 autoscraper: try_consume raised: %s", e)
            allowed = True
        if not allowed:
            skips["token_bucket_denied"] = int(skips.get("token_bucket_denied", 0)) + 1
            out["skipped"] += 1
            log.info("🔍🚦 autoscraper: nhentai tokens empty, yielding to users (id=%s)", gid)
            await asyncio.sleep(rest)
            continue

        # Fetch the detail via the SAME sync wrapper the route uses so a
        # successful fetch lands in Turso under the exact same key.
        try:
            detail = scraper._direct_nhentai_detail(gid)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            detail = None
            _state["last_error"] = f"detail fetch {gid}: {e}"[:200]
            log.warning("🔍⚠️ autoscraper: fetch id=%s crashed: %s", gid, e)

        if isinstance(detail, dict) and detail.get("id"):
            # v12.13 (#D): confirm schema-complete AND that the cache write
            # actually landed (paranoid readback).
            if not _is_schema_complete(detail):
                skips["upstream_detail_empty"] = int(skips.get("upstream_detail_empty", 0)) + 1
                out["skipped"] += 1
                # v12.14: log which fields the payload actually has so we
                # can never again mis-diagnose a schema mismatch as an
                # 'upstream empty'. If this line ever fires with a rich
                # key list, the schema gate is wrong — not the network.
                try:
                    keys = sorted(list(detail.keys()))[:12]
                except Exception:  # noqa: BLE001
                    keys = []
                log.warning(
                    "🔍⚠️ autoscraper: id=%s answered but payload incomplete; keys=%s",
                    gid, keys,
                )
            else:
                try:
                    readback = cache.get(gkey, allow_stale=False)
                except Exception:  # noqa: BLE001
                    readback = None
                if readback is None:
                    skips["cache_write_failed"] = int(skips.get("cache_write_failed", 0)) + 1
                    out["failed"] += 1
                    log.warning("🔍💀 autoscraper: id=%s fetched OK but cache refused the write", gid)
                else:
                    out["done"] += 1
                    log.info("🔍✅ autoscraper: id=%s saved to Turso", gid)
        else:
            skips["upstream_detail_empty"] = int(skips.get("upstream_detail_empty", 0)) + 1
            out["failed"] += 1
            log.warning("🔍⚠️ autoscraper: id=%s returned empty from nhentai", gid)

        # Polite rest between gallery fetches (user-requested).
        await asyncio.sleep(rest)

    _state["skip_reasons"] = skips
    # v12.14: per-page summary line so a Render log tail reads like a story.
    log.info(
        "🔍📊 autoscraper: %s p%s summary done=%d skipped=%d failed=%d",
        sort, page, out["done"], out["skipped"], out["failed"],
    )
    return out


async def _scrape_tag_page_details(tag: str, page: int) -> Dict[str, int]:
    """v12.15 (Phase 2): hydrate details for every card on a TAG search page.

    Mirrors _scrape_page_details but sources the card list from a live
    nhentai tag search (`_direct_nhentai_search`) instead of the cached
    sort-page. Consumes the `search` bucket for the list call and the
    `galleries` bucket for each detail fetch, so user traffic still wins.
    Never raises.
    """
    cache = _get_cache()
    scraper = _get_scraper()
    out = {"done": 0, "skipped": 0, "failed": 0}
    if cache is None or scraper is None:
        return out

    _state["current_sort"] = f"tag:{tag}"
    _state["current_page"] = page

    # Token-bucket guard for the SEARCH list call itself.
    try:
        allowed = bool(cache.try_consume("search", cost=1.0))
    except Exception:  # noqa: BLE001
        allowed = True
    if not allowed:
        skips = _state.get("skip_reasons") or _fresh_skip_counters()
        skips["token_bucket_denied"] = int(skips.get("token_bucket_denied", 0)) + 1
        _state["skip_reasons"] = skips
        out["skipped"] += 1
        log.info("🔍🚦 autoscraper: search tokens empty, tag '%s' p%s deferred", tag, page)
        return out

    try:
        items = scraper._direct_nhentai_search(tag, page, "popular")  # noqa: SLF001
    except Exception as e:  # noqa: BLE001
        items = None
        _state["last_error"] = f"tag search {tag}:{page}: {e}"[:200]
        log.warning("🔍⚠️ autoscraper: tag '%s' p%s search failed: %s", tag, page, e)

    if not isinstance(items, list) or not items:
        skips = _state.get("skip_reasons") or _fresh_skip_counters()
        skips["no_search_page_cached"] = int(skips.get("no_search_page_cached", 0)) + 1
        _state["skip_reasons"] = skips
        out["skipped"] += 1
        log.info("🔍⏳ autoscraper: tag '%s' p%s returned no cards", tag, page)
        return out

    log.info("🔍📄 autoscraper: scanning tag '%s' page %s (%d cards)", tag, page, len(items))

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
        try:
            existing = cache.get(gkey, allow_stale=False)
        except Exception:  # noqa: BLE001
            existing = None
        if existing is not None and _is_schema_complete(existing):
            skips["already_cached_fresh"] = int(skips.get("already_cached_fresh", 0)) + 1
            out["skipped"] += 1
            continue
        try:
            allowed = bool(cache.try_consume("galleries", cost=1.0))
        except Exception:  # noqa: BLE001
            allowed = True
        if not allowed:
            skips["token_bucket_denied"] = int(skips.get("token_bucket_denied", 0)) + 1
            out["skipped"] += 1
            await asyncio.sleep(rest)
            continue
        try:
            detail = scraper._direct_nhentai_detail(gid)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            detail = None
            _state["last_error"] = f"detail fetch {gid}: {e}"[:200]
            log.warning("🔍⚠️ autoscraper: fetch id=%s crashed: %s", gid, e)
        if isinstance(detail, dict) and detail.get("id") and _is_schema_complete(detail):
            out["done"] += 1
            log.info("🔍✅ autoscraper: id=%s saved to Turso (tag '%s')", gid, tag)
        else:
            skips["upstream_detail_empty"] = int(skips.get("upstream_detail_empty", 0)) + 1
            out["failed"] += 1
        await asyncio.sleep(rest)

    _state["skip_reasons"] = skips
    log.info(
        "🔍📊 autoscraper: tag '%s' p%s summary done=%d skipped=%d failed=%d",
        tag, page, out["done"], out["skipped"], out["failed"],
    )
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
            log.exception("🔍⚠️ autoscraper: priority page %s:%s crashed: %s", sort, page, e)
        _state["galleries_done_this_run"]    += int(res.get("done", 0))
        _state["galleries_skipped_this_run"] += int(res.get("skipped", 0))
        _state["galleries_failed_this_run"]  += int(res.get("failed", 0))
        walked += 1

    # v12.15: TWO-PHASE BREADTH-FIRST SWEEP.
    # Load the durable sweep position (Mongo) so a Render restart doesn't
    # reset the walker to (popular-today, page 1). Phase 1 walks the 4
    # sorts × 30 pages breadth-first; Phase 2 walks the popular-tag list
    # × 5 pages each; on full completion the admin gets ONE Telegram
    # alert and the sweep loops back to Phase 1.
    sweep = _load_sweep()
    _state["sweep_phase"] = sweep["phase"]
    _state["sweep_sort_idx"] = sweep["sort_idx"]
    _state["sweep_page"] = sweep["page"]
    _state["sweep_tag_idx"] = sweep["tag_idx"]
    _state["sweep_tag_page"] = sweep["tag_page"]
    _state["sweep_sweeps_completed"] = sweep["sweeps_completed"]

    # Skip-fast advance: if the walker lands on a fully-cached page,
    # advance to the next pair in the SAME tick instead of burning the
    # whole tick. Bounded by SKIP_FAST_CAP so the worker still has head-
    # room for real user traffic.
    advances = 0
    while walked < PAGES_PER_TICK and advances < SKIP_FAST_CAP:
        if sweep["phase"] == 1:
            pair = _next_sort_pair(sweep)
            if pair is None:
                # Phase 1 done → transition to Phase 2.
                sweep["phase"] = 2
                sweep["tag_idx"] = 0
                sweep["tag_page"] = 1
                log.info("🔍🎯 autoscraper: Phase 1 complete, switching to Phase 2 (tag sweep)")
                _save_sweep(sweep)
                continue
            sort, page = pair
            _state["current_sort"] = sort
            _state["current_page"] = page
            try:
                res = await _scrape_page_details(sort, page)
            except Exception as e:  # noqa: BLE001
                res = {"done": 0, "skipped": 0, "failed": 0}
                _state["last_error"] = f"page {sort}:{page} raised: {e}"[:200]
                log.exception("🔍⚠️ autoscraper: page %s:%s crashed: %s", sort, page, e)
            _state["galleries_done_this_run"]    += int(res.get("done", 0))
            _state["galleries_skipped_this_run"] += int(res.get("skipped", 0))
            _state["galleries_failed_this_run"]  += int(res.get("failed", 0))
            walked += 1
            advances += 1
            _save_sweep(sweep)
            # Skip-fast: if everything on the page was already cached,
            # advance again in this same tick instead of idling.
            skip_reasons = _state.get("skip_reasons") or {}
            if (
                int(res.get("done", 0)) == 0
                and int(res.get("failed", 0)) == 0
                and int(skip_reasons.get("already_cached_fresh", 0)) > 0
                and int(skip_reasons.get("no_search_page_cached", 0)) == 0
            ):
                log.info("🔍⏭️ autoscraper: %s p%s fully cached — advancing to next pair", sort, page)
                continue  # loop back and pick the next pair
            break  # real work happened — end the tick
        else:
            # Phase 2 — tag sweep.
            scraper = _get_scraper()
            pair = _next_tag_pair(sweep, scraper)
            if pair is None:
                # Phase 2 done — fire admin alert ONCE, then loop back
                # to Phase 1 for a fresh sweep (user's choice).
                if not sweep.get("alert_sent"):
                    alert_text = (
                        "🔍🎉 Autoscraper finished the full sweep!\n\n"
                        f"Phase 1: 4 sorts × {PAGE_CAP} pages — done.\n"
                        f"Phase 2: {len(sweep.get('tags_active', []))} tags × {TAG_PAGE_CAP} pages — done.\n\n"
                        "Looping back to Phase 1 for a fresh sweep now."
                    )
                    sent = await _send_admin_alert(alert_text)
                    sweep["alert_sent"] = True
                    if not sent:
                        log.warning("🔍⚠️ autoscraper: full-sweep alert failed to send")
                # Reset for the next sweep.
                sweep["phase"] = 1
                sweep["sort_idx"] = 0
                sweep["page"] = 1
                sweep["tag_idx"] = 0
                sweep["tag_page"] = 1
                sweep["tags_done"] = []
                sweep["tags_active"] = list(DEFAULT_TAGS)
                sweep["sweeps_completed"] = int(sweep.get("sweeps_completed", 0)) + 1
                sweep["alert_sent"] = False
                log.info("🔍🔄 autoscraper: sweep #%d starting fresh from Phase 1",
                         sweep["sweeps_completed"])
                _save_sweep(sweep)
                continue
            tag, page = pair
            _state["current_sort"] = f"tag:{tag}"
            _state["current_page"] = page
            try:
                res = await _scrape_tag_page_details(tag, page)
            except Exception as e:  # noqa: BLE001
                res = {"done": 0, "skipped": 0, "failed": 0}
                _state["last_error"] = f"tag page {tag}:{page} raised: {e}"[:200]
                log.exception("🔍⚠️ autoscraper: tag %s:%s crashed: %s", tag, page, e)
            _state["galleries_done_this_run"]    += int(res.get("done", 0))
            _state["galleries_skipped_this_run"] += int(res.get("skipped", 0))
            _state["galleries_failed_this_run"]  += int(res.get("failed", 0))
            walked += 1
            advances += 1
            _save_sweep(sweep)
            # Skip-fast: same logic as Phase 1.
            skip_reasons = _state.get("skip_reasons") or {}
            if (
                int(res.get("done", 0)) == 0
                and int(res.get("failed", 0)) == 0
                and int(skip_reasons.get("already_cached_fresh", 0)) > 0
                and int(skip_reasons.get("no_search_page_cached", 0)) == 0
            ):
                log.info("🔍⏭️ autoscraper: tag '%s' p%s fully cached — advancing", tag, page)
                continue
            break

    # Update the live-state dict so the admin panel shows the CURRENT
    # sweep position (not the position at the start of the tick).
    _state["sweep_phase"] = sweep["phase"]
    _state["sweep_sort_idx"] = sweep["sort_idx"]
    _state["sweep_page"] = sweep["page"]
    _state["sweep_tag_idx"] = sweep["tag_idx"]
    _state["sweep_tag_page"] = sweep["tag_page"]
    _state["sweep_sweeps_completed"] = sweep["sweeps_completed"]

    _state["finished_at"] = int(time.time())
    _state["phase"] = "idle"
    _persist_state()
    _done   = _state["galleries_done_this_run"]
    _skip   = _state["galleries_skipped_this_run"]
    _fail   = _state["galleries_failed_this_run"]
    _emoji  = "🎉" if _done > 0 else ("💤" if _fail == 0 else "⚠️")
    log.info(
        "🔍%s autoscraper: tick end phase=%s current=%s p%s done=%d skipped=%d failed=%d advances=%d",
        _emoji, sweep["phase"], _state["current_sort"], _state["current_page"],
        _done, _skip, _fail, advances,
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
