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
PREFETCH_MAX_PAGES:    int   = _env_int("PREFETCH_MAX_PAGES",    10)
PREFETCH_DELAY_SEC:    float = _env_float("PREFETCH_DELAY_SEC",  1.0)
PREFETCH_ENABLED:      bool  = _env_bool("PREFETCH_ENABLED",     True)


def _enabled() -> bool:
    """Master switch: env flag AND at least one sort configured.

    Cheap enough to call every loop iteration; run_forever() re-reads
    this so an admin toggling the env var + restarting Render can
    disable the sweep without a code deploy.
    """
    return bool(PREFETCH_ENABLED and _SORTS and PREFETCH_MAX_PAGES > 0)


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


async def prefetch_once() -> Dict[str, Any]:
    """Run ONE full sweep across every (sort, page) tuple.

    Never raises. Every fetch failure is counted in ``_last_run`` and
    the sweep keeps going. Returns the fresh ``last_run_summary()``
    dict at the end so ``/prefetch now`` can echo it back to the admin.
    """
    if not _enabled():
        _last_run["enabled"] = False
        return last_run_summary()

    cache = _get_cache_module()
    paths = _bootstrap_paths()

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
    log.info(
        "prefetch: sweep end ok=%d skipped=%d failed=%d dur=%ss",
        _last_run["pages_ok"], _last_run["pages_skipped"],
        _last_run["pages_failed"], _last_run["duration_sec"],
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
    """Sleep / sweep / sleep loop.

    * Sleeps ``PREFETCH_INTERVAL_SEC`` between sweeps (default 6 h).
    * ``trigger_now()`` can wake this loop early via ``_wake_event``.
    * Re-reads ``_enabled()`` each tick so ops can disable the sweep
      without a code deploy — just flip ``PREFETCH_ENABLED=0`` in the
      Render env and bounce the worker.
    * NEVER raises to the caller. worker.py spawns this as a task and
      isn't watching for exceptions; a crash here would just silently
      stop the sweep, which is worse than logging + continuing.
    """
    log.info(
        "prefetch: run_forever start interval=%ss enabled=%s",
        PREFETCH_INTERVAL_SEC, _enabled(),
    )
    wake = _get_wake_event()
    lock = _get_run_lock()

    while True:
        try:
            if _enabled():
                async with lock:
                    await prefetch_once()
            else:
                log.debug("prefetch: disabled by env — idle tick")
        except asyncio.CancelledError:
            log.info("prefetch: run_forever cancelled — stopping")
            raise
        except Exception as e:  # noqa: BLE001
            _last_run["last_error"] = f"sweep crashed: {e!s}"[:200]
            log.exception("prefetch: sweep crashed (continuing): %s", e)

        # Sleep the interval, but wake early if trigger_now() set the event.
        try:
            await asyncio.wait_for(wake.wait(), timeout=PREFETCH_INTERVAL_SEC)
            wake.clear()
            log.info("prefetch: woken early by trigger_now()")
        except asyncio.TimeoutError:
            pass  # normal interval expiry
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
