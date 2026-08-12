"""
prefetch_cron.py — v12.4 background prefetch sweep (Turso cache warmer).

Purpose
-------
Once every PREFETCH_INTERVAL_SEC (default: 6 h), sweep the popular /
date / popular-today / popular-week nhentai listings across pages
1..PREFETCH_MAX_PAGES, PUT each response into the shared Turso+Mongo
blob cache under the same cache-key convention the request-time code
already uses.

Design constraints
------------------
1. NEVER starves user traffic. Every prefetch call goes through the
   SAME shared token bucket the mini-app uses; if try_consume() says
   no, we skip that page (do NOT sleep-loop the bucket dry).
2. NEVER blocks the worker event loop. run_forever() is an
   asyncio.create_task() spawned by worker.py right before the main
   poll loop starts.
3. Turso outage tolerant. If turso_available() flips False mid-sweep,
   we still write to Mongo — the cache-put helper handles the
   fallback internally.
4. Anon quotas only. No NHENTAI_API_KEY assumed. Per openapi.json:
   /search is 10/min anon, /galleries is 20/min anon. The 1 s
   PREFETCH_DELAY_SEC + shared bucket gate keeps us well under.
5. Deterministic, cheap re-emit. Everything is env-tunable so the
   ops surface for the user is one Render env panel, no code changes.

Public surface (imported by admin_bot.py /prefetch commands)
------------------------------------------------------------
    run_forever()          coroutine, wired into worker.py at boot
    prefetch_once()        one full sweep of all sorts × pages
    last_run_summary()     dict snapshot used by /prefetch status
    trigger_now()          one-shot manual kick from /prefetch now

The scaffold below (35 %) intentionally has NO fetching logic yet —
only the constants, env plumbing, and the module-level _last_run
dict every later function reads/writes. This lets 45 % / 55 %
introduce the fetch + loop code as pure functions on top of a
stable state surface.
"""
from __future__ import annotations

import os
import time
import logging
from typing import Dict, Any, List, Tuple, Optional

log = logging.getLogger("miniapp.prefetch")

# v12.8: emoji-tagged sweep telemetry, greppable in Render logs.
_LOG_SWEEP_WRITE = "📝 [TURSO WRITE] prefetch sweep uploaded  key=%s  bytes=%s"
_LOG_SWEEP_SKIP  = "⏭  [PREFETCH SKIP] bucket exhausted      key=%s"
_LOG_SWEEP_429   = "🚫 [PREFETCH 429] upstream rate-limited  key=%s"

# Upstream endpoint + UA mirror what scraper_bridge already uses at request
# time. Kept in sync intentionally: if the user rotates UAs later they can
# grep for a single string.
_NH_API = "https://nhentai.net/api/v2"
_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "DoujinshiUniverse/12.4 (+https://github.com/vairegi/mtproto-userbot)"
)

# Bucket the shared token bucket already tracks for /api/v2/search.
# 10/min anon (openapi.json). Prefetch consumes the SAME bucket as user
# traffic — this is intentional; it's the whole reason prefetch never
# starves users.
_BUCKET_SEARCH = "search"

# Fetch timeout. Small enough that a hung upstream call doesn't stall
# the sweep for whole minutes; large enough for a cold nhentai response.
_FETCH_TIMEOUT_SEC = 15.0

# ---------------------------------------------------------------------------
# Env-tunable knobs. Defaults chosen to sit comfortably below anon quotas
# (openapi.json: /search 10/min anon, /galleries 20/min anon).
# ---------------------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("prefetch: bad int for %s=%r — using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("prefetch: bad float for %s=%r — using default %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in ("0", "false", "no", "off")


# Sorts we warm. Order matters for the /prefetch status log (round-robin).
# Keep this list SMALL — every sort × page is one anon /galleries call.
_SORTS: Tuple[str, ...] = (
    "popular",
    "date",
    "popular-today",
    "popular-week",
)

PREFETCH_INTERVAL_SEC: int   = _env_int("PREFETCH_INTERVAL_SEC", 6 * 60 * 60)  # 6 h
# v12.18: default raised 10 → 20 pages per sort. The user observed
# "Popular page 5 still misses after 2 sweeps". Root cause was NOT the
# cache itself — pages 1..10 were warm, but the DETAILS scraper (which
# the admin status screen reports on) writes `gallery:<id>` rows, not
# `search:<sort>:page<N>` rows, so "sweeps done=2" there proves nothing
# about the list-page bucket. Raising the LIST warmer's depth gives the
# real fix. 4 sorts × 20 pages × ~10 KB payload ≈ 800 KB in Turso,
# zero extra Render RAM (payloads are never held beyond one page fetch).
PREFETCH_MAX_PAGES:    int   = _env_int("PREFETCH_MAX_PAGES",    20)
PREFETCH_DELAY_SEC:    float = _env_float("PREFETCH_DELAY_SEC",  1.0)
PREFETCH_ENABLED:      bool  = _env_bool("PREFETCH_ENABLED",     True)

# v12.18: Mongo-backed priority queue for 429-skipped (sort, page) tuples.
# Before this fix, a page that hit a 429 during a sweep was silently
# stranded until the sort's next staggered interval (up to 6 hours for
# "popular"). Now the sweep re-enqueues the tuple so the NEXT 5-minute
# tick retries it FIRST, before walking the regular (sort, page) walk.
_PERSIST_KEY = "prefetch_priority_v1"

# v12.10 (#2): per-sort stagger. Each sort has its own interval so the
# hottest buckets (popular-today: 3 h) refresh more often than the slower
# ones (popular: 6 h). run_forever() ticks at TICK_INTERVAL_SEC and only
# sweeps a sort whose (now - last_run) >= its bucket. Env override:
# PREFETCH_INTERVAL_<SORT-UPPER-UNDERSCORED>_SEC, e.g.
# PREFETCH_INTERVAL_POPULAR_TODAY_SEC=1800.
def _default_stagger_for(sort: str) -> int:
    return {
        "popular":       6 * 60 * 60,
        "date":          5 * 60 * 60,
        "popular-week":  4 * 60 * 60,
        "popular-today": 3 * 60 * 60,
    }.get(sort, PREFETCH_INTERVAL_SEC)


def _env_stagger_for(sort: str) -> int:
    key = "PREFETCH_INTERVAL_" + sort.upper().replace("-", "_") + "_SEC"
    return _env_int(key, _default_stagger_for(sort))


_STAGGER: Dict[str, int] = {s: _env_stagger_for(s) for s in _SORTS}

# How often run_forever() wakes to check per-sort due-times. Small enough
# to keep due-times reasonably honest, large enough to be a no-op on cost.
TICK_INTERVAL_SEC: int = _env_int("PREFETCH_TICK_SEC", 5 * 60)  # 5 min


def _enabled() -> bool:
    """Master switch: env flag AND at least one sort configured.

    Cheap enough to call every loop iteration; run_forever() re-reads
    this so an admin toggling the env var + restarting Render can
    disable the sweep without a code deploy.
    """
    return bool(PREFETCH_ENABLED and _SORTS and PREFETCH_MAX_PAGES > 0)


# ---------------------------------------------------------------------------
# v12.18: Mongo-backed priority queue for 429-skipped / bucket-starved pages.
# Persisted via miniapp db settings (same pattern as details_prefetch_cron)
# so the queue survives Render restarts. Fail-open: any Mongo hiccup means
# the sweep just behaves like v12.17 (no re-enqueue) — never fatal.
# ---------------------------------------------------------------------------
_PRIORITY_CAP = 40


def _db_set(key: str, value: Any) -> None:
    try:
        from .. import db as _midb
        _midb.set_setting(key, value)
    except Exception as e:  # noqa: BLE001
        log.debug("prefetch: db_set(%s) failed: %s", key, e)


def _db_get(key: str, default: Any = None) -> Any:
    try:
        from .. import db as _midb
        return _midb.get_setting(key, default)
    except Exception as e:  # noqa: BLE001
        log.debug("prefetch: db_get(%s) failed: %s", key, e)
        return default


def _priority_push(sort: str, page: int) -> None:
    """Re-enqueue a (sort, page) tuple that was skipped (429 / dry bucket)
    so the next tick retries it first. Deduped + capped."""
    try:
        lst = _db_get(_PERSIST_KEY, []) or []
        if not isinstance(lst, list):
            lst = []
        entry = [str(sort), int(page)]
        if entry in lst:
            return
        if len(lst) >= _PRIORITY_CAP:
            lst = lst[-(_PRIORITY_CAP - 1):]   # keep the freshest entries
        lst.append(entry)
        _db_set(_PERSIST_KEY, lst)
    except Exception as e:  # noqa: BLE001
        log.debug("prefetch: priority_push failed: %s", e)


def _priority_pop_all() -> List[Tuple[str, int]]:
    """Drain the priority queue (read + clear). Returns [] on any error."""
    try:
        lst = _db_get(_PERSIST_KEY, []) or []
        if not lst:
            return []
        _db_set(_PERSIST_KEY, [])
        out: List[Tuple[str, int]] = []
        for e in lst:
            try:
                s, p = str(e[0]), int(e[1])
                if s in _SORTS and 1 <= p <= max(PREFETCH_MAX_PAGES, 1):
                    out.append((s, p))
            except Exception:  # noqa: BLE001
                continue
        return out
    except Exception as e:  # noqa: BLE001
        log.debug("prefetch: priority_pop_all failed: %s", e)
        return []


def _bootstrap_paths() -> List[Tuple[str, int]]:
    """Enumerate the (sort, page) tuples one full sweep will touch.

    Returned in the order the sweep will visit them. 45 % introduces
    a helper (_cache_key_for) that turns each tuple into the same
    string key nhentai_cache uses request-time so a prefetched entry
    is a straight hit for the very next user.
    """
    out: List[Tuple[str, int]] = []
    for sort in _SORTS:
        for page in range(1, PREFETCH_MAX_PAGES + 1):
            out.append((sort, page))
    return out


# ---------------------------------------------------------------------------
# Cross-call state. Read by /prefetch status; written by prefetch_once().
# A plain dict is enough — the sweep is single-tasked, no lock needed.
# ---------------------------------------------------------------------------
_last_run: Dict[str, Any] = {
    "started_at":     None,   # epoch seconds of most recent sweep start
    "finished_at":    None,   # epoch seconds of most recent sweep finish
    "duration_sec":   None,   # finished_at - started_at, when finished
    "sorts_planned":  len(_SORTS),
    "pages_planned":  len(_SORTS) * PREFETCH_MAX_PAGES,
    "pages_ok":       0,      # cache PUT succeeded
    "pages_skipped":  0,      # bucket said no, or upstream 429
    "pages_failed":   0,      # exception / non-2xx / bad JSON
    "last_error":     None,   # str, most recent failure reason
    "sweep_count":    0,      # total sweeps completed since boot
    "enabled":        _enabled(),
}

# v12.10 (#2): per-sort last-run epoch (0 == never). run_forever() reads
# this every tick to decide which sorts are due. prefetch_once(only_sorts=)
# updates the entries it actually touches.
_last_run_per_sort: Dict[str, int] = {s: 0 for s in _SORTS}


def last_run_summary() -> Dict[str, Any]:
    """Return a defensive copy of _last_run for the admin /prefetch cmd.

    Kept synchronous + allocation-cheap because admin_bot handlers call
    it from a Telegram callback path where speed matters more than
    throughput.
    """
    snap = dict(_last_run)
    # Refresh the enabled bit on read so an ops toggle is visible
    # without waiting for the next sweep to run.
    snap["enabled"] = _enabled()
    snap["interval_sec"] = PREFETCH_INTERVAL_SEC
    snap["max_pages"]    = PREFETCH_MAX_PAGES
    snap["delay_sec"]    = PREFETCH_DELAY_SEC
    snap["sorts"]        = list(_SORTS)
    snap["now"]          = int(time.time())
    # v12.10 (#2): per-sort schedule so /prefetch status can print each
    # sort's own last-run + next-run-in.
    now_i = snap["now"]
    per_sort: Dict[str, Dict[str, Any]] = {}
    for s in _SORTS:
        interval = _STAGGER.get(s, PREFETCH_INTERVAL_SEC)
        last = _last_run_per_sort.get(s, 0) or 0
        next_due = (last + interval) if last else now_i
        per_sort[s] = {
            "interval_sec":  interval,
            "last_run":      last or None,
            "next_run_at":   next_due,
            "next_run_in":   max(0, next_due - now_i),
        }
    snap["per_sort"] = per_sort
    return snap


# ---------------------------------------------------------------------------
# 45 % — pure helpers.
#
# _cache_key_for : (sort, page) -> stable string. Uses the "search:" prefix
#                  that nhentai_cache.ttl_for_key already recognises as
#                  TTL_SEARCH_SEC (3 days) — so a prefetched row expires
#                  on the same schedule as a user-driven one, and Turso
#                  writes land in the exact table + column layout every
#                  request-time reader hits.
# _fetch_one_page: async httpx call to /api/v2/search with the SAME
#                  parameter shape scraper_bridge._direct_nhentai_search
#                  uses. Empty query resolves to "english" for the trending
#                  path, matching the mini-app's English-only spirit.
#
# Neither helper touches the cache directly — that's prefetch_once()'s job
# at 55 %. Splitting responsibilities keeps both pure enough to unit-test
# with a stub httpx if the user ever asks for coverage.
# ---------------------------------------------------------------------------
def _cache_key_for(sort: str, page: int) -> str:
    """Deterministic cache key for a (sort, page) sweep entry.

    Format: ``search:<sort>:page<N>``.

    * ``search:`` prefix is what ``nhentai_cache.ttl_for_key`` treats as
      ``TTL_SEARCH_SEC``; the prefetched row expires on the same 3-day
      schedule as a user-driven search.
    * ``sort`` is lower-cased and stripped so a caller passing
      ``"Popular "`` produces the same key as ``"popular"``.
    * ``page`` is coerced to int; a value < 1 clamps to 1 so a bad env
      var can't accidentally cache junk under key ``page0``.
    """
    s = (sort or "").strip().lower() or "popular"
    try:
        p = int(page)
    except (TypeError, ValueError):
        p = 1
    if p < 1:
        p = 1
    return f"search:{s}:page{p}"


async def _fetch_one_page(sort: str, page: int) -> Optional[dict]:
    """Fetch one nhentai /api/v2/search page. Return the parsed JSON dict
    on 2xx, or ``None`` on any error (429, network, non-JSON, etc.).

    Mirrors ``scraper_bridge._direct_nhentai_search``'s parameter shape
    exactly:

    * ``query=english`` when caller sent an empty string — nhentai requires
      *some* query, and "english" returns the huge trending pool the
      mini-app already surfaces.
    * ``sort`` is passed through the same allow-list as request-time.
    * ``page`` is coerced to int and clamped ≥1.

    Failure semantics
    -----------------
    * Never raises. On any error returns ``None`` and lets the sweep
      accumulate a ``pages_failed`` / ``pages_skipped`` counter.
    * A 429 is logged at INFO (not WARNING) — the sweep is background
      traffic, an occasional 429 is expected and self-heals on the
      next tick.
    * A malformed JSON body counts as a failure — we NEVER put non-dict
      payloads into the cache.
    """
    # Late import so the module still imports cleanly in test envs that
    # don't ship httpx (e.g. minimal compileall workers).
    try:
        import httpx  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        log.warning("prefetch: httpx not importable — skipping fetch (%s)", e)
        return None

    sort_map = {
        "popular":       "popular",
        "popular-week":  "popular-week",
        "popular-today": "popular-today",
        "date":          "date",
        "recent":        "date",
        "":              "popular",
    }
    real_sort = sort_map.get((sort or "").strip().lower(), "popular")

    try:
        p = int(page)
    except (TypeError, ValueError):
        p = 1
    if p < 1:
        p = 1

    params = {"query": "english", "sort": real_sort, "page": p}
    headers = {
        "User-Agent": _UA,
        "Accept":     "application/json",
        "Referer":    "https://nhentai.net/",
    }

    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_SEC) as client:
            r = await client.get(f"{_NH_API}/search", params=params, headers=headers)
    except Exception as e:  # noqa: BLE001
        _last_run["last_error"] = f"net: {e!s}"[:200]
        log.info("prefetch: network error sort=%s page=%s: %s", real_sort, p, e)
        return None

    if r.status_code == 429:
        _last_run["last_error"] = f"429 sort={real_sort} page={p}"
        log.info("prefetch: upstream 429 sort=%s page=%s — skipping", real_sort, p)
        return None
    if r.status_code >= 400:
        _last_run["last_error"] = f"HTTP {r.status_code} sort={real_sort} page={p}"
        log.info(
            "prefetch: upstream HTTP %s sort=%s page=%s — skipping",
            r.status_code, real_sort, p,
        )
        return None

    try:
        payload = r.json()
    except Exception as e:  # noqa: BLE001
        _last_run["last_error"] = f"json: {e!s}"[:200]
        log.info("prefetch: bad JSON sort=%s page=%s: %s", real_sort, p, e)
        return None

    if not isinstance(payload, dict):
        _last_run["last_error"] = f"non-dict payload sort={real_sort} page={p}"
        log.info("prefetch: non-dict payload sort=%s page=%s", real_sort, p)
        return None

    return payload


# ---------------------------------------------------------------------------
# 55 % — sweep + loop.
#
# prefetch_once() walks _bootstrap_paths() in order and, for each tuple:
#   1. Asks the shared token bucket for permission via nhentai_cache.
#      * bucket says NO — count as skipped, continue immediately.
#      * bucket says YES — fetch, write to cache, sleep PREFETCH_DELAY_SEC.
#   2. Between pages: honor PREFETCH_DELAY_SEC (default 1 s) so we're a
#      good API citizen even when the bucket has slack.
#
# run_forever() is the boot-time coroutine worker.py spawns. It sleeps
# PREFETCH_INTERVAL_SEC between sweeps and re-reads _enabled() each tick
# so ops can flip PREFETCH_ENABLED=0 + restart Render to disable us
# without a code deploy.
#
# Both funcs write _last_run in-place so /prefetch status reflects the
# most recent state even mid-sweep. Single-tasked — no lock needed.
# ---------------------------------------------------------------------------
import asyncio

# Late import of the cache module so a broken nhentai_cache doesn't
# stop worker.py from booting entirely. We probe once at first use.
_cache_mod = None


def _get_cache_module():
    """Import nhentai_cache lazily; cache the reference on first success.

    Returns the module object or ``None`` if it can't be imported (e.g.
    a stripped-down test env). Callers must tolerate ``None`` — in that
    case we still fetch, just don't PUT anywhere.
    """
    global _cache_mod
    if _cache_mod is not None:
        return _cache_mod
    try:
        from . import nhentai_cache as _nc  # noqa: WPS433
        _cache_mod = _nc
    except Exception as e:  # noqa: BLE001
        log.warning("prefetch: nhentai_cache import failed — running cache-less (%s)", e)
        _cache_mod = None
    return _cache_mod


async def prefetch_once(only_sorts: Optional[List[str]] = None) -> Dict[str, Any]:
    """Run ONE full sweep.

    v12.10 (#2): when ``only_sorts`` is given, only those sorts are swept
    (used by run_forever() to honor per-sort stagger). Default None keeps
    the historical behavior — sweep every sort. /prefetch now still calls
    this with no args so the admin "force everything" contract is intact.
    """
    if not _enabled():
        _last_run["enabled"] = False
        return last_run_summary()

    cache = _get_cache_module()
    # v12.10 (#2): filter paths by ``only_sorts`` when the caller asked
    # for a partial sweep. An unknown sort in only_sorts is dropped
    # silently (fail-open — never let a bad env value stall the loop).
    if only_sorts:
        _requested = {s for s in only_sorts if s in _SORTS}
        paths = [(s, p) for (s, p) in _bootstrap_paths() if s in _requested]
        _swept = [s for s in _SORTS if s in _requested]
    else:
        paths = _bootstrap_paths()
        _swept = list(_SORTS)

    # v12.18: drain the Mongo-backed priority queue FIRST — these are
    # (sort, page) tuples that hit a 429 or a dry bucket on a previous
    # tick. Retrying them before the regular walk means a skipped page
    # is re-attempted within ~5 minutes instead of waiting out the
    # sort's full staggered interval (up to 6 h for "popular").
    priority_paths = _priority_pop_all()
    if priority_paths:
        log.info("prefetch: draining %d priority entries before regular walk",
                 len(priority_paths))
    # Priority entries go first, then the regular bootstrap paths minus
    # any tuple already covered by the priority drain (no double-fetch).
    prio_set = set(priority_paths)
    paths = priority_paths + [p for p in paths if p not in prio_set]

    _last_run["started_at"]    = int(time.time())
    _last_run["finished_at"]   = None
    _last_run["duration_sec"]  = None
    _last_run["sorts_planned"] = len(_SORTS)
    _last_run["pages_planned"] = len(paths)
    _last_run["pages_ok"]      = 0
    _last_run["pages_skipped"] = 0
    _last_run["pages_failed"]  = 0
    _last_run["last_error"]    = None
    _last_run["enabled"]       = True

    log.info(
        "prefetch: sweep begin sorts=%s pages_per_sort=%d total=%d",
        list(_SORTS), PREFETCH_MAX_PAGES, len(paths),
    )

    for sort, page in paths:
        # Users first: never starve the bucket. try_consume() returns
        # False when there's no token left; we then skip — do NOT
        # sleep-loop the bucket dry.
        allowed = True
        if cache is not None:
            try:
                allowed = bool(cache.try_consume(_BUCKET_SEARCH, cost=1.0))
            except Exception as e:  # noqa: BLE001
                # A cache-layer bug must not kill the sweep. Log + carry on
                # (fail-open matches the mini-app's own contract at 30 %).
                log.debug("prefetch: try_consume raised: %s", e)
                allowed = True

        if not allowed:
            _last_run["pages_skipped"] += 1
            log.debug(
                "prefetch: bucket said no (sort=%s page=%s) — yielding to users",
                sort, page,
            )
            _priority_push(sort, page)   # v12.18: re-enqueue for next tick
            # Still sleep the polite interval so we don't spin the bucket.
            await asyncio.sleep(PREFETCH_DELAY_SEC)
            continue

        payload = await _fetch_one_page(sort, page)
        if payload is None:
            # _fetch_one_page already recorded last_error. Distinguish
            # 429s (counted as "skipped" — upstream told us to wait) from
            # hard errors (counted as "failed").
            err = _last_run.get("last_error") or ""
            if err.startswith("429"):
                _last_run["pages_skipped"] += 1
                _priority_push(sort, page)   # v12.18: re-enqueue for next tick
            else:
                _last_run["pages_failed"] += 1
            await asyncio.sleep(PREFETCH_DELAY_SEC)
            continue

        # Cache PUT. best-effort; a False return isn't fatal — we still
        # spent the fetch, and the next tick will try again.
        if cache is not None:
            key = _cache_key_for(sort, page)
            try:
                ok = bool(cache.put(key, payload))
            except Exception as e:  # noqa: BLE001
                log.debug("prefetch: cache.put(%s) raised: %s", key, e)
                ok = False
            if ok:
                _last_run["pages_ok"] += 1
                try:
                    import json as _json
                    _bytes = len(_json.dumps(payload, default=str))
                except Exception:  # noqa: BLE001
                    _bytes = -1
                log.info(_LOG_SWEEP_WRITE, key, _bytes)
            else:
                _last_run["pages_failed"] += 1
                _last_run["last_error"] = f"cache put failed for {key}"
        else:
            # Cache module absent: we still fetched successfully; count
            # it as ok so the operator sees the network path is healthy.
            _last_run["pages_ok"] += 1

        await asyncio.sleep(PREFETCH_DELAY_SEC)

    _last_run["finished_at"]  = int(time.time())
    _last_run["duration_sec"] = _last_run["finished_at"] - _last_run["started_at"]
    _last_run["sweep_count"] += 1
    # v12.10 (#2): stamp per-sort last-run for exactly the sorts we
    # actually swept this pass. run_forever() uses these to schedule
    # the next partial sweep per its per-sort stagger.
    _now_i = _last_run["finished_at"]
    for _s in _swept:
        _last_run_per_sort[_s] = _now_i
    log.info(
        "prefetch: sweep end ok=%d skipped=%d failed=%d dur=%ss sorts=%s",
        _last_run["pages_ok"], _last_run["pages_skipped"],
        _last_run["pages_failed"], _last_run["duration_sec"], _swept,
    )
    return last_run_summary()


# One-shot trigger primitive used by /prefetch now. asyncio.Event lets an
# already-running run_forever() wake up early instead of waiting out its
# PREFETCH_INTERVAL_SEC sleep. Created lazily to bind to the correct loop.
_wake_event: Optional[asyncio.Event] = None
_run_lock: Optional[asyncio.Lock] = None


def _get_wake_event() -> asyncio.Event:
    global _wake_event
    if _wake_event is None:
        _wake_event = asyncio.Event()
    return _wake_event


def _get_run_lock() -> asyncio.Lock:
    global _run_lock
    if _run_lock is None:
        _run_lock = asyncio.Lock()
    return _run_lock


async def run_forever() -> None:
    """Tick / stagger / sweep loop.

    v12.10 (#2): wakes every ``TICK_INTERVAL_SEC`` (default 5 min) and
    sweeps only the sorts whose per-sort stagger has elapsed since
    ``_last_run_per_sort``. The old "one big 6 h sweep" behavior is
    preserved when every sort's stagger elapses on the same tick, so
    /prefetch now still works exactly the same.

    * ``trigger_now()`` can wake this loop early via ``_wake_event``.
    * Re-reads ``_enabled()`` each tick so ops can disable the sweep
      without a code deploy.
    * NEVER raises to the caller.
    """
    log.info(
        "prefetch: run_forever start tick=%ss stagger=%s enabled=%s",
        TICK_INTERVAL_SEC, _STAGGER, _enabled(),
    )
    wake = _get_wake_event()
    lock = _get_run_lock()

    while True:
        try:
            if _enabled():
                now_i = int(time.time())
                # v12.10 (#2): a sort is due when its last-run is 0 (never)
                # OR when (now - last_run) >= its per-sort stagger.
                due = [
                    s for s in _SORTS
                    if (_last_run_per_sort.get(s, 0) == 0)
                    or (now_i - _last_run_per_sort[s] >= _STAGGER.get(s, PREFETCH_INTERVAL_SEC))
                ]
                # A manual /prefetch now trigger via _wake_event should
                # sweep EVERYTHING, matching the pre-v12.10 contract.
                if wake.is_set():
                    due = list(_SORTS)
                    wake.clear()
                if due:
                    async with lock:
                        await prefetch_once(only_sorts=due)
                else:
                    log.debug("prefetch: no sorts due this tick")
            else:
                log.debug("prefetch: disabled by env — idle tick")
        except asyncio.CancelledError:
            log.info("prefetch: run_forever cancelled — stopping")
            raise
        except Exception as e:  # noqa: BLE001
            _last_run["last_error"] = f"sweep crashed: {e!s}"[:200]
            log.exception("prefetch: sweep crashed (continuing): %s", e)

        # v12.10 (#2): sleep ONE TICK, not the whole interval — per-sort
        # due-times are what actually decide the next sweep. wake_event
        # still shortcuts a manual /prefetch now.
        try:
            await asyncio.wait_for(wake.wait(), timeout=TICK_INTERVAL_SEC)
            log.info("prefetch: woken early by trigger_now()")
        except asyncio.TimeoutError:
            pass  # normal tick expiry
        except asyncio.CancelledError:
            log.info("prefetch: run_forever cancelled during sleep — stopping")
            raise


async def trigger_now() -> Dict[str, Any]:
    """Manual kick from ``/prefetch now``.

    If a sweep is already running (``_run_lock`` held), we return the
    current summary without launching a second concurrent sweep — the
    already-running one is exactly what the admin wanted anyway.
    Otherwise we run ONE sweep inline and return its summary.
    """
    lock = _get_run_lock()
    if lock.locked():
        log.info("prefetch: trigger_now while sweep in progress — skipping duplicate")
        return last_run_summary()

    # Ask run_forever() to wake early too, so its interval timer resets
    # from "now" instead of from "whenever the last scheduled tick was".
    try:
        _get_wake_event().set()
    except Exception:  # noqa: BLE001
        pass

    async with lock:
        return await prefetch_once()
