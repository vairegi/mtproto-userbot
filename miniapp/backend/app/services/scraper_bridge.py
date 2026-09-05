"""
scraper_bridge.py — Adapter around hf_scraper.py (parent project).

Isolates the Mini App from hf_scraper's API surface. All hf_scraper
functions are ASYNC (async def search, async def fetch_gallery_meta,
async def health_check). This bridge exposes SYNC wrappers by calling
asyncio.run() on each async call — FastAPI's def handlers run in a
threadpool so a fresh event loop is safe.

Return-shape adapter:
  * hf_scraper.search() returns Optional[SearchPage] (a dataclass) whose
    .hits is List[SearchHit] (a dataclass). We flatten those into plain
    dicts the frontend expects: {id, title, cover, pages, tags}.
  * hf_scraper.fetch_gallery_meta() returns Optional[GalleryMeta]
    (a dataclass with title/tags/cover_url/pages/gallery_id).

Fallback tree:
  1. If the caller provided a non-empty query, prefer hf_scraper.search
     (respects its cache, filters English-only via tag id 12227).
  2. If the query is empty (Popular/Recent chips), hf_scraper.search
     returns None by design — go straight to a direct nhentai call so
     the default Discover view is populated.
  3. If EITHER path raises, fall through to the direct nhentai call so
     the frontend never sees a 500.
"""
from __future__ import annotations

import os  # v1.22.5: USE_OLD_CACHE env toggle

import asyncio
import dataclasses
import logging
import os
import sys
import threading
import time as _time
from typing import Any, Optional

log = logging.getLogger("miniapp.scraper")

# v12.34b: cross-bot user-hint hook. Imported lazily via the
# `_bot0_hints()` accessor below so an import failure (e.g. deployment
# without bot0_hints.py) can never break a Mini App request — the worst
# case is the hint never gets pushed and BOT 1 stays on its regular
# round-robin schedule, identical to v12.34.
def _bot0_hints():
    try:
        import bot0_hints as _bh  # noqa: WPS433
        return _bh
    except Exception:  # noqa: BLE001
        return None


def _hint_push(gid) -> None:
    bh = _bot0_hints()
    if bh is None:
        return
    try:
        bh.hint_push_gid(gid)
    except Exception:  # noqa: BLE001
        # Never raise into the request hot path.
        pass
# `grep '[TURSO CACHE HIT]'` catches BOTH the sync (scraper_bridge) and
# async (hf_scraper) paths in the same Render log window.
_LOG_HIT    = "⚡ [TURSO CACHE HIT] Served data from Turso  key=%s"
_LOG_STALE  = "⚠ [TURSO STALE HIT] Served STALE data (upstream 429/down)  key=%s"
_LOG_MISS   = "🌐 [CACHE MISS] Fetched from upstream nhentai and cached to Turso  key=%s"
_LOG_WRITE  = "📝 [TURSO WRITE] Uploaded payload to Turso  key=%s  bytes=%s"
_LOG_DEDUP  = "🤝 [TURSO DEDUP] payload unchanged — skipped rewrite  key=%s"


def _sb_turso_cache():
    """Lazy import of nhentai_cache; None if not importable.

    scraper_bridge is imported by workers, tests, and FastAPI routes;
    a cache import failure must never break request handling.
    """
    try:
        from . import nhentai_cache as _nhc
        return _nhc
    except Exception:  # noqa: BLE001
        return None

# Add the parent project on sys.path so `import hf_scraper` works when the
# Mini App is deployed alongside admin_bot.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BOT_ROOT = os.environ.get("MINIAPP_BOT_ROOT")
_CANDIDATES = [
    _BOT_ROOT,
    os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..")),
    "/opt/render/project/src",
]
for p in _CANDIDATES:
    if p and os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

try:
    import hf_scraper as _hf   # noqa: E402
    HAVE_HF = True
    log.info("hf_scraper imported successfully")
except Exception as e:  # noqa: BLE001
    _hf = None
    HAVE_HF = False
    log.warning("hf_scraper not importable — using fallback nhentai client (%s)", e)


# ---------------------------------------------------------------------------
# Async → sync helper — PERSISTENT PER-THREAD EVENT LOOP
# ---------------------------------------------------------------------------
# BUG 3 fix: hf_scraper keeps a pooled httpx.AsyncClient bound to whatever
# event loop it first ran on. asyncio.run() creates + CLOSES a fresh loop
# on every call, so the second call on this thread hit
#   WARNING:hf_scraper: Event loop is closed
# The fix is to keep a single event loop alive per thread for the whole
# FastAPI process lifetime, and reuse it on every _run_async() call.

_loop_holder: threading.local = threading.local()


def _get_loop() -> asyncio.AbstractEventLoop:
    loop = getattr(_loop_holder, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _loop_holder.loop = loop
    return loop


def _run_async(coro):
    """Run an async coroutine on a persistent per-thread event loop.

    Avoids 'Event loop is closed' warnings from hf_scraper's pooled
    httpx.AsyncClient by NEVER closing the loop between calls.
    """
    return _get_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Direct nhentai fallback (used for empty/popular queries + on error)
# ---------------------------------------------------------------------------
import httpx

# nhentai retired the legacy /api/galleries/search endpoint — it now returns
# 403 Forbidden (confirmed against live traffic 2026-08). Every direct call
# must go through /api/v2/*.
_NH_API = "https://nhentai.net/api/v2"
_ENGLISH_TAG_ID = 12227   # matches hf_scraper's filter

# v11.2: soft 429 back-off cache. Keyed by (query, sort, page) for
# _direct_nhentai_search and by ("detail", gallery_id) for
# _direct_nhentai_detail. Values are absolute expiry timestamps. When a
# key is present and not yet expired, the direct call short-circuits
# with an empty result instead of hitting nhentai again — the exact bug
# in the user's log (dozens of ERROR + full traceback per second under 429).
# v11.6 hardening (same rationale as hf_scraper._RATE_LIMIT_*):
#   * Base TTL raised 30s -> 60s.
#   * Exponential ramp on repeat 429s per key (cap 300s).
#   * Honour the server's `Retry-After` header when present.
#   * Env-tunable: NH_RATE_LIMIT_TTL_SEC / NH_RATE_LIMIT_TTL_CAP_SEC /
#     NH_RATE_LIMIT_RAMP.
import os as _os_rl
_RATE_LIMIT_CACHE: dict = {}
_RATE_LIMIT_STRIKES: dict = {}
try:
    _RATE_LIMIT_TTL_SEC = int(_os_rl.environ.get("NH_RATE_LIMIT_TTL_SEC", "60"))
except (TypeError, ValueError):
    _RATE_LIMIT_TTL_SEC = 60
try:
    _RATE_LIMIT_TTL_CAP_SEC = int(_os_rl.environ.get("NH_RATE_LIMIT_TTL_CAP_SEC", "300"))
except (TypeError, ValueError):
    _RATE_LIMIT_TTL_CAP_SEC = 300
try:
    _RATE_LIMIT_RAMP = float(_os_rl.environ.get("NH_RATE_LIMIT_RAMP", "2.0"))
except (TypeError, ValueError):
    _RATE_LIMIT_RAMP = 2.0

# v12.18 (audit fix 3a): bound the two rate-limit dicts so a long-lived
# backend process can never accumulate unbounded search keys. Both dicts
# used to prune ONLY on the specific read paths that checked ban expiry;
# keys that were never looked up again lived forever.
try:
    _RL_CACHE_MAX_ENTRIES = int(_os_rl.environ.get("NH_RATE_LIMIT_CACHE_MAX", "512"))
except (TypeError, ValueError):
    _RL_CACHE_MAX_ENTRIES = 512
try:
    _RL_CACHE_SWEEP_EVERY = int(_os_rl.environ.get("NH_RATE_LIMIT_CACHE_SWEEP_EVERY", "100"))
except (TypeError, ValueError):
    _RL_CACHE_SWEEP_EVERY = 100
_rl_writes_since_sweep = 0


def _rl_sweep_expired():
    """Drop every _RATE_LIMIT_CACHE entry whose ban is already in the past,
    and any orphan _RATE_LIMIT_STRIKES entry whose paired ban is gone.
    Cheap: both dicts are capped at 512 entries."""
    now = _time.time()
    dead = [k for k, v in _RATE_LIMIT_CACHE.items() if not (v and v > now)]
    for k in dead:
        _RATE_LIMIT_CACHE.pop(k, None)
        _RATE_LIMIT_STRIKES.pop(k, None)
    # Also drop any strike entries whose ban has fully expired — those are
    # only useful while the ban is live for the exponential ramp.
    stale_strikes = [k for k in list(_RATE_LIMIT_STRIKES.keys())
                     if k not in _RATE_LIMIT_CACHE]
    for k in stale_strikes:
        _RATE_LIMIT_STRIKES.pop(k, None)


def _rl_enforce_caps():
    """If either dict is over the cap, drop the OLDEST entries first.
    Called immediately after every write."""
    while len(_RATE_LIMIT_CACHE) > _RL_CACHE_MAX_ENTRIES:
        k = next(iter(_RATE_LIMIT_CACHE))   # dicts preserve insertion order
        _RATE_LIMIT_CACHE.pop(k, None)
        _RATE_LIMIT_STRIKES.pop(k, None)
    while len(_RATE_LIMIT_STRIKES) > _RL_CACHE_MAX_ENTRIES:
        k = next(iter(_RATE_LIMIT_STRIKES))
        _RATE_LIMIT_STRIKES.pop(k, None)


def _rl_cache_set(cache_key, expires_at):
    """Write a ban timestamp; run the periodic sweep + hard cap."""
    global _rl_writes_since_sweep
    _RATE_LIMIT_CACHE[cache_key] = expires_at
    _rl_writes_since_sweep += 1
    if _rl_writes_since_sweep >= _RL_CACHE_SWEEP_EVERY:
        _rl_writes_since_sweep = 0
        _rl_sweep_expired()
    _rl_enforce_caps()


def _rl_strikes_set(cache_key, value):
    """Write a strike counter; enforce the hard cap."""
    _RATE_LIMIT_STRIKES[cache_key] = value
    _rl_enforce_caps()


def _rate_limit_backoff_sec(cache_key, retry_after):
    """Compute the next back-off duration for a rate-limited key. See
    hf_scraper._rate_limit_backoff_sec for the design doc."""
    if retry_after:
        try:
            ra = int(float(str(retry_after).strip()))
            return max(_RATE_LIMIT_TTL_SEC, min(_RATE_LIMIT_TTL_CAP_SEC, ra))
        except (TypeError, ValueError):
            pass
    strikes = _RATE_LIMIT_STRIKES.get(cache_key, 0)
    dur = _RATE_LIMIT_TTL_SEC * (_RATE_LIMIT_RAMP ** strikes)
    _rl_strikes_set(cache_key, strikes + 1)
    return int(max(_RATE_LIMIT_TTL_SEC, min(_RATE_LIMIT_TTL_CAP_SEC, dur)))
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

# v12.69: private-key parity with hf_scraper / hf_scraper_lite. When
# NHENTAI_API_KEY is set, EVERY upstream call from this module must send
# `Authorization: Key <token>` — without it nhentai returns a stub
# response (title/tags only, no `media_id`, no `images.pages`), which
# silently killed pages_meta on both `_direct_nhentai_detail` (write path)
# and `_pages_meta_backfill` (read path). Read at import time; empty when
# unset so anonymous callers still work.
_NHENTAI_API_KEY = os.environ.get("NHENTAI_API_KEY", "").strip()


def _nh_headers() -> dict:
    """Shared header dict for every direct nhentai HTTP call in this module.

    Includes Authorization when NHENTAI_API_KEY is configured — without it
    the v2 payload loses `media_id` + `images.pages`, so the working
    page1_url path AND the reader's pages_meta go silently empty.
    """
    h = {
        "User-Agent": _UA,
        "Accept":     "application/json",
        "Referer":    "https://nhentai.net/",
    }
    if _NHENTAI_API_KEY:
        h["Authorization"] = f"Key {_NHENTAI_API_KEY}"
    return h


import re

_EVENT_PREFIX = re.compile(r"^\([A-Za-z0-9+\- ]+\)\s*")
_BRACKET_TAIL = re.compile(r"(\[[^\]]*\])\s*$")
_T_CDN = "https://t.nhentai.net"


def clean_title(raw: str) -> str:
    """Strip leading event tags '(C92)' and trailing meta brackets
    '[English] [Scans]' from an nhentai title. Returns a human-friendly
    short title for the card grid; the FULL titles remain available on the
    detail sheet via the v2 detail endpoint."""
    s = (raw or "").strip()
    s = _EVENT_PREFIX.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _BRACKET_TAIL.sub("", s).strip()
    return s or (raw or "").strip()


def _title_from_item(item: dict) -> str:
    """v2 search rows expose english_title / japanese_title (plain strings).
    Older v1 rows exposed a title dict; handle both for safety."""
    et = item.get("english_title")
    if isinstance(et, str) and et.strip():
        return clean_title(et)
    jt = item.get("japanese_title")
    if isinstance(jt, str) and jt.strip():
        return clean_title(jt)
    t = item.get("title")
    if isinstance(t, dict):
        return clean_title(t.get("english") or t.get("pretty") or t.get("japanese") or "")
    if isinstance(t, str):
        return clean_title(t)
    return ""


def _notify_details_scraper(sort, page) -> None:
    """v12.11 (#1b): best-effort ping to details_prefetch_cron so the page
    the user just opened gets its cards' details hydrated on the very next
    tick. No-op when the module is missing (older deploys) or the call
    fails — the scraper is opportunistic, never load-bearing."""
    try:
        from . import details_prefetch_cron as _dpc  # noqa: WPS433
        _dpc.notify_page(sort, page)
    except Exception:  # noqa: BLE001
        pass


def _title_en_clean_from_item(item: dict) -> str:
    """v12.10 (#8): cleaned English title for the card GRID only.

    Source of truth is the upstream's own `title.english` (or the v2 row's
    `english_title` plain string), run through clean_title() to strip
    '[artist]' prefixes and '[English] [Scans]' tails. Falls back to the
    general cleaned title so a card NEVER renders an empty caption.
    """
    t = item.get("title")
    if isinstance(t, dict):
        et = t.get("english")
        if isinstance(et, str) and et.strip():
            return clean_title(et)
    et = item.get("english_title")
    if isinstance(et, str) and et.strip():
        return clean_title(et)
    return _title_from_item(item)


def _thumb_url_from_item(item: dict) -> str:
    """Build the cover/thumbnail URL. v2 search rows give `thumbnail` as a
    CDN-relative path like 'galleries/1200622/thumb.png' (extension varies)."""
    thumb = item.get("thumbnail")
    if isinstance(thumb, str) and thumb.strip():
        return _T_CDN + "/" + thumb.strip().lstrip("/")
    # Legacy v1 shape (images.cover.t + media_id) — kept for safety.
    media_id = item.get("media_id") or ""
    images = item.get("images") or {}
    cover = images.get("cover") or images.get("thumbnail") or {}
    ext_map = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
    ext = ext_map.get(cover.get("t", "j"), "jpg")
    return f"{_T_CDN}/galleries/{media_id}/cover.{ext}"


def _direct_nhentai_search(q: str, page: int, sort: str) -> list[dict]:
    """
    Direct call to nhentai's JSON API. Used when:
      * caller sent an empty query (hf_scraper won't accept it)
      * hf_scraper raised an exception
      * hf_scraper isn't importable in this deployment
    """
    # Empty query + Popular chip → use the "popular" sort with a wildcard.
    # nhentai's own frontend uses the same trick: an empty search with
    # sort=popular returns the trending page.
    sort_map = {
        "popular":       "popular",
        "popular-week":  "popular-week",
        "popular-today": "popular-today",
        "date":          "date",
        "recent":        "date",
        "":              "popular",
        None:            "popular",
    }
    real_sort = sort_map.get((sort or "").lower(), "popular")

    # nhentai requires SOME query; when the user typed nothing we ask for
    # "english" which returns huge trending list. That matches the
    # English-only spirit of the Mini App exactly.
    # v12.29: normalize the typed query (lowercase + whitespace-collapse)
    # BEFORE building the Turso key — byte-identical to hf_scraper.search()
    # v12.19 and BOT 1's cache.bot0_search_key. Without this, a typed
    # query like "Sole  Female" produced a different key than BOT 1's warm
    # row and ALWAYS missed the cache (and the 429 back-off cache, which
    # is keyed by the same tuple).
    query = " ".join(q.lower().split()) if q else "english"

    params = {"query": query, "sort": real_sort, "page": int(page or 1)}

    # v11.2: 429 back-off — short-circuit while the ban is live so we
    # don't hammer upstream and don't dump a full traceback per request.
    cache_key = ("search", query, real_sort, int(page or 1))
    now = _time.time()
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    if ban and ban > now:
        return []

    # v12.9 Turso READ. Empty-query sort chips use the short
    # `search:<sort>:pageN` form that prefetch_cron already writes, so a
    # user hit lands DIRECTLY on the row the prefetch warmed. User-typed
    # queries use a distinct but still-deterministic tail.
    # v12.33c: chip reads use query=english; BOT 1 now warms with
    # language:english. Treat BOTH as the chip namespace so reads hit the
    # warmed `search:<sort>:pageN` rows either way.
    if query in ("english", "language:english"):
        _turso_key = f"search:{real_sort}:page{int(page or 1)}"
    else:
        _turso_key = f"search:q={query}|sort={real_sort}|page={int(page or 1)}"
    _nhc = _sb_turso_cache()
    if _nhc is not None:
        # v1.22.5: USE_OLD_CACHE=1 (default) lets chip/sort pages be served
        # from stale-but-present cache rows when their TTL has lapsed.
        # Rows are never deleted (cache-never-expires design), so gating
        # reads on freshness produced the "gray next button after page 2"
        # bug: aged rows read as MISSes, the upstream nhentai fallback was
        # 429-backoff'd, and has_more flipped False. Stale cards are always
        # better than a dead pagination bar — ScraperBot re-freshes rows on
        # its schedule. Set USE_OLD_CACHE=0 to restore strict-fresh reads.
        _allow_stale = os.environ.get("USE_OLD_CACHE", "1").strip() not in (
            "0", "false", "no")
        try:
            _hit = _nhc.get(_turso_key, allow_stale=_allow_stale)
        except Exception:  # noqa: BLE001
            _hit = None
        if isinstance(_hit, list):
            log.info(_LOG_HIT, _turso_key)
            # v12.11 (#1b): user is looking at this page NOW — hydrate its
            # card details on the next scraper tick (empty-query chip pages
            # only; those map 1:1 to the cron's sort/page walk).
            if query in ("english", "language:english"):
                _notify_details_scraper(real_sort, int(page or 1))
            return _hit

    try:
        # v2 endpoint: /api/v2/search (params: query, sort, page)
        # v12.69: parity with the two /galleries/<id> call sites — send
        # NHENTAI_API_KEY when configured so search responses come back
        # authenticated (avoids stubbed anonymous payloads on this route
        # too, if nhentai ever tightens it).
        r = httpx.get(
            f"{_NH_API}/search",
            params=params,
            headers=_nh_headers(),
            timeout=15,
        )
        # v11.2: 429 is expected under load. Log at WARNING level ONCE
        # (not ERROR + full traceback every request) and cache the ban.
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After") if hasattr(r, "headers") else None
            dur = _rate_limit_backoff_sec(cache_key, retry_after)
            _rl_cache_set(cache_key, now + dur)   # v12.18: bounded write
            log.warning(
                "nhentai HTTP 429 for /search q=%r sort=%r page=%s — "
                "backing off for %ss%s", q, real_sort, page, dur,
                f" (Retry-After={retry_after})" if retry_after else "",
            )
            return []
        # v11.6: success resets the strike counter for this key.
        if 200 <= r.status_code < 300:
            _RATE_LIMIT_STRIKES.pop(cache_key, None)
        r.raise_for_status()
        data = r.json() or {}
    except httpx.HTTPStatusError as e:
        # Any other 4xx/5xx: log once at warning, no traceback.
        log.warning(
            "nhentai search HTTP %s for q=%r sort=%r: %s",
            getattr(e.response, "status_code", "?"), q, real_sort, e,
        )
        return []
    except Exception as e:  # noqa: BLE001
        # Network / DNS / timeout: warn without a full stack trace so the
        # log stays readable.
        log.warning("direct nhentai search failed q=%r sort=%r: %s", q, real_sort, e)
        return []

    out: list[dict] = []
    for item in data.get("result") or []:
        # English-only filter (matches hf_scraper's behaviour).
        tag_ids = item.get("tag_ids") or []
        if _ENGLISH_TAG_ID not in tag_ids:
            continue
        out.append({
            "id":    item.get("id"),
            "title": _title_from_item(item),
            # v12.10 (#8): grid-only cleaned English title. Frontend falls
            # back to `title` when this is empty/missing.
            "title_en_clean": _title_en_clean_from_item(item),
            "cover": _thumb_url_from_item(item),
            "pages": item.get("num_pages"),
            "tags":  [{"name": t.get("name"), "type": t.get("type")}
                      for t in item.get("tags") or []],
        })

    # v12.9 Turso WRITE. best-effort; nhentai_cache.put() writes to BOTH
    # Turso and Mongo (v12.4 semantics), so a Turso outage still leaves a
    # Mongo copy. We only cache non-empty results so a transient 0-row
    # nhentai response never poisons the cache.
    if out and _nhc is not None:
        try:
            import json as _json
            _bytes = len(_json.dumps(out, default=str))
        except Exception:  # noqa: BLE001
            _bytes = -1
        log.info(_LOG_MISS, _turso_key)
        try:
            _ok = _nhc.put(_turso_key, out)
            if _ok == "unchanged":
                log.info(_LOG_DEDUP, _turso_key)
            elif _ok:
                log.info(_LOG_WRITE, _turso_key, _bytes)
                # v12.34b: tell BOT 1 the user actually wants each row in
                # this page so its next details tick warms the gallery.<id>
                # rows. Fire-and-forget; capped queue, gravity-trimmed.
                try:
                    for _item in out:
                        _hint_push(_item.get("id"))
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            log.debug("turso write failed for %s: %s", _turso_key, e)

    # v12.9: if upstream returned nothing (transient failure) AND Turso
    # has a stale copy, serve the stale copy so users aren't left with a
    # blank grid. TTL_STALE_GRACE_SEC (7 days) governs how old that copy
    # can be — see nhentai_cache.py.
    if not out and _nhc is not None:
        try:
            _stale = _nhc.get(_turso_key, allow_stale=True)
        except Exception:  # noqa: BLE001
            _stale = None
        if isinstance(_stale, list) and _stale:
            log.info(_LOG_STALE, _turso_key)
            if query in ("english", "language:english"):
                _notify_details_scraper(real_sort, int(page or 1))
            return _stale
    # v12.11 (#1b): fresh fetch path — notify on a non-empty result too.
    if out and query == "english":
        _notify_details_scraper(real_sort, int(page or 1))
    return out


def _group_tags(item: dict) -> dict:
    """Group the v2 detail tags by type so the frontend can render labelled
    rows: artist / parody / character / group / tag / language / category."""
    groups: dict = {}
    for t in item.get("tags") or []:
        if not isinstance(t, dict):
            continue
        typ = str(t.get("type") or "tag")
        nm = str(t.get("name") or "").strip()
        if not nm:
            continue
        groups.setdefault(typ, []).append(nm)
    return groups


def _iso_date(ts) -> str:
    try:
        import datetime
        return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


# v11: nhentai image-extension code -> file extension (mirror of the
# table in hf_scraper._NH_EXT_MAP; kept here so the direct-detail path
# also builds page-1 URLs without importing hf_scraper's private symbol).
_NH_EXT_MAP = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}


def _pages_meta_from_raw(item: dict) -> list:
    """v12.70: build the reader's pages_meta ([{n, path, w, h}]) from a RAW
    nhentai /api/v2/galleries/<id> payload.

    The REAL v2 payload (verified live 2026-09-05) has NO 'images' key at
    all — the page list is the TOP-LEVEL 'pages' array, and each entry
    already carries a full CDN-relative 'path' (plus width/height):
        pages[0] = {"number": 1, "path": "galleries/4160409/1.webp",
                    "width": 1280, "height": 1808, ...}
    v12.68 read item["images"]["pages"] — a key that never exists — so it
    returned [] on EVERY real payload (32/32 live failures). We now read
    the top-level 'pages' first, fall back to the legacy 'images.pages'
    shape just in case an old cached row was written that way, and prefer
    the API's own 'path' string over rebuilding it from media_id.

    `path` is CDN-relative ('galleries/<media_id>/<n>.<ext>') — the reader
    prepends the i1-i4.nhentai.net host itself (plugins/reader.js pageUrl).
    The images stay on nhentai's CDN (zero Render bandwidth/RAM); we only
    store this tiny per-page metadata inside the gallery cache row.
    Returns [] when the payload genuinely carries no page list.
    """
    if not isinstance(item, dict):
        return []
    # Real v2 shape: top-level 'pages'. Legacy/defensive: 'images.pages'.
    src = item.get("pages")
    if not (isinstance(src, list) and src):
        images = item.get("images") or {}
        src = images.get("pages") if isinstance(images, dict) else None
    if not (isinstance(src, list) and src):
        return []
    media_id = str(item.get("media_id") or "").strip()
    out = []
    for i, p in enumerate(src):
        if not isinstance(p, dict):
            continue
        n = int(p.get("number") or p.get("n") or i + 1)
        # Prefer the API's own full path; only rebuild from media_id +
        # <n>.<ext> for legacy entries that carry a 't' ext-code instead.
        path = str(p.get("path") or "").strip()
        if not path and media_id:
            ext = _NH_EXT_MAP.get(str(p.get("t") or "j").strip().lower(), "jpg")
            path = f"galleries/{media_id}/{n}.{ext}"
        if not path:
            continue
        out.append({
            "n":    n,
            "path": path,
            "w":    int(p.get("width")  or p.get("w") or 0),
            "h":    int(p.get("height") or p.get("h") or 0),
        })
    return out


def _direct_nhentai_page1(item: dict) -> str:
    """Build the high-quality page-1 URL from a raw nhentai detail dict.

    v12.70: the REAL v2 payload has no 'images' key — the page list is the
    TOP-LEVEL 'pages' array whose entries already carry a full CDN-relative
    'path'. The old version read item["images"]["pages"], which is None on
    every real response, so this helper silently returned '' on every fresh
    write (page1_url has been empty in the cache the whole time; the
    frontend just fell back to cover_thumb, so nobody noticed).
    Returns '' when the payload genuinely carries no page list.
    """
    src = item.get("pages")
    if not (isinstance(src, list) and src):
        images = item.get("images") or {}
        src = images.get("pages") if isinstance(images, dict) else None
    if not (isinstance(src, list) and src):
        return ""
    first = src[0] if isinstance(src[0], dict) else {}
    path = str(first.get("path") or "").strip()
    if path:
        return f"https://i.nhentai.net/{path.lstrip('/')}"
    # Legacy/defensive: entry has a 't' ext-code but no full 'path'.
    media_id = str(item.get("media_id") or "").strip()
    if not media_id:
        return ""
    ext = _NH_EXT_MAP.get(str(first.get("t") or "j").strip().lower(), "jpg")
    n = int(first.get("number") or first.get("n") or 1)
    return f"https://i.nhentai.net/galleries/{media_id}/{n}.{ext}"


def _direct_nhentai_detail(gallery_id: str) -> dict:
    # v11.2: 429 back-off cache (same rationale as _direct_nhentai_search).
    cache_key = ("detail", str(gallery_id))
    now = _time.time()
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    if ban and ban > now:
        return {}

    # v12.9 Turso READ for gallery detail. Cache-key format triggers
    # nhentai_cache.ttl_for_key -> TTL_GALLERY_SEC (30 days). We cache
    # the NORMALISED dict this function returns (not raw upstream JSON),
    # so a hit skips network + parsing + tag grouping in one shot.
    _detail_turso_key = f"gallery:{gallery_id}"
    _nhc = _sb_turso_cache()
    if _nhc is not None:
        try:
            _hit = _nhc.get(_detail_turso_key, allow_stale=False)
        except Exception as e:  # noqa: BLE001
            log.warning("nhc.get(%s) raised: %s", _detail_turso_key, e)
            _hit = None
        if isinstance(_hit, dict) and _hit.get("id"):
            log.info(_LOG_HIT, _detail_turso_key)
            return _hit
        # v12.34c: log when the read returned but the gate at line 579
        # rejected it — disambiguates cold-miss from bad-payload.
        if _hit is not None:
            log.warning(
                "nhc.get(%s) returned type=%s (truthy=%s) but failed "
                "'isinstance dict + has id' gate — refetching from nhentai",
                _detail_turso_key, type(_hit).__name__, bool(_hit),
            )

    try:
        # v2 endpoint for a single gallery: /api/v2/galleries/<id>
        # v12.69: use _nh_headers() so NHENTAI_API_KEY (when set) is sent
        # — anonymous responses omit media_id/images.pages, which is what
        # was silently corrupting every write into the cache.
        r = httpx.get(
            f"{_NH_API}/galleries/{gallery_id}",
            headers=_nh_headers(),
            timeout=15,
        )
        # v11.2: 429 -> log ONCE at WARNING + back-off, no stack trace.
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After") if hasattr(r, "headers") else None
            dur = _rate_limit_backoff_sec(cache_key, retry_after)
            _rl_cache_set(cache_key, now + dur)   # v12.18: bounded write
            log.warning(
                "nhentai HTTP 429 for /galleries/%s — backing off for %ss%s",
                gallery_id, dur,
                f" (Retry-After={retry_after})" if retry_after else "",
            )
            return {}
        # v11.6: success resets the strike counter for this key.
        if 200 <= r.status_code < 300:
            _RATE_LIMIT_STRIKES.pop(cache_key, None)
        r.raise_for_status()
        item = r.json() or {}
    except httpx.HTTPStatusError as e:
        log.warning(
            "nhentai detail HTTP %s for id=%r: %s",
            getattr(e.response, "status_code", "?"), gallery_id, e,
        )
        return {}
    except Exception as e:  # noqa: BLE001
        log.warning("direct nhentai detail failed id=%r: %s", gallery_id, e)
        return {}

    # --- caption fields (power the detail-sheet caption UI) -----------------
    title_obj = item.get("title") or {}
    english_full = title_obj.get("english") or "" if isinstance(title_obj, dict) else ""
    japanese_full = title_obj.get("japanese") or "" if isinstance(title_obj, dict) else ""
    pretty = (title_obj.get("pretty") or "") if isinstance(title_obj, dict) else ""

    cover_path = (item.get("cover") or {}).get("path") or ""
    cover_thumb = _T_CDN + "/" + cover_path.lstrip("/") if cover_path else _thumb_url_from_item(item)
    # v11: prefer page 1 as the cover image (i.nhentai.net/.../1.<ext> is
    # served at full resolution vs t.nhentai.net/.../cover.jpg.webp).
    page1 = _direct_nhentai_page1(item)
    cover = page1 or cover_thumb

    groups = _group_tags(item)
    flat_tags = [{"name": n, "type": typ} for typ, names in groups.items() for n in names]

    _detail_out = {
        "id":       item.get("id"),
        "title":    clean_title(pretty) if pretty else _title_from_item(item),
        "title_english":  english_full,
        "title_japanese": japanese_full,
        "cover":    cover,
        # v11: expose page1_url separately alongside `cover`.
        "page1_url": page1,
        "pages":    item.get("num_pages"),
        # v12.68: reader page metadata — built from the SAME raw upstream
        # payload (images.pages + media_id), zero extra requests. Every new
        # gallery:<id> cache row is born with pages_meta so Read works on
        # first tap. Canonical normalize.py already passes it through.
        "pages_meta": _pages_meta_from_raw(item),
        "favorites": item.get("num_favorites"),
        "upload_date": _iso_date(item.get("upload_date")),
        "scanlator": item.get("scanlator") or "",
        "tags":     flat_tags,
        "tag_groups": groups,
    }

    # v12.9 Turso WRITE for gallery detail. 30-day TTL (ttl_for_key
    # matches the `gallery:` prefix). `_nhc` resolved at top of this fn.
    if _detail_out.get("id") and _nhc is not None:
        try:
            import json as _json
            _bytes = len(_json.dumps(_detail_out, default=str))
        except Exception:  # noqa: BLE001
            _bytes = -1
        log.info(_LOG_MISS, _detail_turso_key)
        try:
            _ok = _nhc.put(_detail_turso_key, _detail_out)
            if _ok == "unchanged":
                log.info(_LOG_DEDUP, _detail_turso_key)
            elif _ok:
                log.info(_LOG_WRITE, _detail_turso_key, _bytes)
                # v12.34b: hint the cross-bot user-hint queue so the NEXT
                # user (and the same user on a refresh) finds this row
                # already in cache. Fire-and-forget — never affects the
                # current request's return value.
                _hint_push(_detail_out.get("id"))
        except Exception as e:  # noqa: BLE001
            log.debug("turso write failed for %s: %s", _detail_turso_key, e)

    return _detail_out


# ---------------------------------------------------------------------------
# Convert hf_scraper dataclass results → plain dicts for the frontend
# ---------------------------------------------------------------------------
def _hit_to_dict(hit) -> dict:
    """Convert a SearchHit dataclass into the frontend's dict shape."""
    if hit is None:
        return {}
    if dataclasses.is_dataclass(hit):
        d = dataclasses.asdict(hit)
    elif isinstance(hit, dict):
        d = hit
    else:
        # Fallback: pluck common attribute names
        d = {
            "gallery_id": getattr(hit, "gallery_id", None),
            "title":      getattr(hit, "title", None),
            "url":        getattr(hit, "url", None),
            "thumb_url":  getattr(hit, "thumb_url", None),
        }
    _title = d.get("title") or ""
    return {
        "id":    d.get("gallery_id") or d.get("id"),
        "title": _title,
        # v12.10 (#8): hf path has no separate English title field, so the
        # cleaned grid title IS the fallback (never empty when title isn't).
        "title_en_clean": clean_title(_title) if _title else "",
        "cover": d.get("thumb_url") or d.get("cover") or d.get("cover_url") or "",
        "pages": d.get("pages") or d.get("num_pages"),
        # v12.66: reader page metadata ([{n,path,w,h}]; [] on old rows)
        "pages_meta": _pages_meta_of(d),
        "tags":  d.get("tags") or [],
    }


def _meta_to_dict(meta) -> dict:
    """Convert a GalleryMeta dataclass into the frontend's dict shape."""
    if meta is None:
        return {}
    if dataclasses.is_dataclass(meta):
        d = dataclasses.asdict(meta)
    elif isinstance(meta, dict):
        d = meta
    else:
        d = {
            "gallery_id": getattr(meta, "gallery_id", None),
            "title":      getattr(meta, "title", None),
            "cover_url":  getattr(meta, "cover_url", None),
            "pages":      getattr(meta, "pages", None),
            "tags":       getattr(meta, "tags", None),
        }
    # hf_scraper's GalleryMeta.tags is List[str]; convert to [{name,type:...}]
    raw_tags = d.get("tags") or []
    tag_dicts = []
    for t in raw_tags:
        if isinstance(t, dict):
            tag_dicts.append(t)
        else:
            tag_dicts.append({"name": str(t), "type": "tag"})
    # v11: hf_scraper.GalleryMeta now carries `page1_url` (the high-quality
    # https://i.nhentai.net/galleries/<media_id>/1.<ext> image). Prefer it
    # for the mini-app card cover; fall back to the traditional thumbnail
    # for legacy / partial payloads that don't have media_id + images.
    page1 = d.get("page1_url") or ""
    cover = page1 or d.get("cover_url") or d.get("cover") or ""
    return {
        "id":    d.get("gallery_id") or d.get("id"),
        "title": d.get("title") or "",
        "cover": cover,
        # v11: expose page1_url separately so consumers that specifically
        # need the full-quality first-page image (e.g. detail-sheet hero,
        # future "reader" preview) can request it without another scrape.
        "page1_url": page1,
        "pages": d.get("pages") or d.get("num_pages"),
        # v12.66: reader page metadata ([{n,path,w,h}]; [] on old rows)
        "pages_meta": _pages_meta_of(d),
        "tags":  tag_dicts,
    }


# ---------------------------------------------------------------------------
# Public API — called by routes/search.py and routes/gallery.py
# ---------------------------------------------------------------------------
# v12.1 (B): the "only 11 results for 'incest'" bug — the English-only tag
# filter drops most of an upstream page, and when upstream page 2 gets
# 429'd the loop used to bail because _direct_nhentai_search returns [] on
# 429 (indistinguishable from "real end of results"). Two fixes:
#   1. Bump _MAX_UPSTREAM_PAGES to 20 so we can actually reach page 50k+.
#   2. Distinguish "soft empty" (429 backoff active) from "hard empty"
#      (upstream really has no more rows) via _direct_nhentai_soft_empty,
#      and on soft-empty SKIP that upstream page instead of stopping.
_MAX_UPSTREAM_PAGES_DEFAULT = 20
_MAX_CONSECUTIVE_SOFT_EMPTY = 3


def _direct_nhentai_soft_empty(q_clean: str, upstream_page: int, sort: str) -> bool:
    """True iff the (query, sort, page) cell is currently rate-limited.
    Lets search() skip a temporarily-banned upstream page and keep going
    instead of bailing at the first 429."""
    sort_map = {"popular": "popular", "popular-week": "popular-week",
                "popular-today": "popular-today", "date": "date",
                "recent": "date", "": "popular", None: "popular"}
    real_sort = sort_map.get((sort or "").lower(), "popular")
    # v12.29: same lowercase+collapse normalization as _direct_nhentai_search
    # so the soft-empty probe checks the SAME back-off key the fetch uses.
    query = " ".join(q_clean.lower().split()) if q_clean else "english"
    cache_key = ("search", query, real_sort, int(upstream_page or 1))
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    return bool(ban and ban > _time.time())


def search(q: str, page: int, sort: str, lang: str,
           include_tags: list[str] | None = None,
           exclude_tags: list[str] | None = None,
           pages_min: int | None = None,
           pages_max: int | None = None,
           per_page: int = 25,
           _return_meta: bool = False):
    """Return a list of normalized gallery dicts (or dict when _return_meta).

    v11.8 (#10): the English-only tag filter drops most of a typical 25-row
    upstream page for niche queries, which used to leave users with only
    8-9 results. Loops upstream pages until we've collected enough
    post-filter rows to satisfy `per_page` (or hit _MAX_UPSTREAM_PAGES).

    v12.1 (B):
      * Survives 429s on individual upstream pages (skip, don't bail).
      * Bumped upstream-page ceiling from 8 → 20 (env-tunable via
        MINIAPP_SEARCH_MAX_UPSTREAM_PAGES).
      * When _return_meta=True, returns a dict
        {items, has_more, upstream_pages_scanned, upstream_rate_limited}
        so the route can drive a Next-Page button honestly.
    """
    include_tags = include_tags or []
    exclude_tags = exclude_tags or []
    # v12.30: lowercase + whitespace-collapse at the TOP so every downstream
    # read (hf branch, direct branch, 429 soft-empty probe, and the Turso
    # key formation inside _direct_nhentai_search) sees the same canonical
    # form BOT 1's cache.bot0_search_key writes. Previously a typed 'Incest'
    # from the UI landed in Turso as ...q=incest|... but any probe /
    # rate-limit key computed against the raw q would look at 'Incest' and
    # miss. Also fixes 'Sole  Female' (double space) diverging from BOT 1's
    # 'sole female' warm row.
    q_clean = " ".join((q or "").lower().split())
    per_page = int(per_page) if per_page and per_page > 0 else 25

    try:
        max_upstream = int(os.environ.get(
            "MINIAPP_SEARCH_MAX_UPSTREAM_PAGES", _MAX_UPSTREAM_PAGES_DEFAULT))
    except (TypeError, ValueError):
        max_upstream = _MAX_UPSTREAM_PAGES_DEFAULT

    start_offset = (max(1, int(page or 1)) - 1) * per_page
    # v1.22.7: collect ONE extra upstream page beyond the window. The
    # v12.30 id-dedup runs AFTER collection, and nhentai's volatile lists
    # (popular-today / popular-week / date) share 5-8 galleries between
    # adjacent pages — so a bare-window collection (50 cards for page 2)
    # shrank below the window after dedup: short pages AND has_more=False
    # (the gray › button on those three sorts while stable all-time
    # 'popular' worked). The extra page is a cache hit when warm, so this
    # costs ~nothing. Loop still stops at max_upstream as before.
    want_total   = start_offset + per_page
    collect_goal = want_total + per_page

    collected: list[dict] = []
    upstream_page = 1
    consecutive_empty = 0
    rate_limited_pages: list[int] = []

    while len(collected) < collect_goal and upstream_page <= max_upstream:
        rows: list[dict] = []
        # v12.29 (SORT BUG FIX): hf_scraper.search() takes NO sort argument
        # and internally hardcodes params["sort"]="date", then serves/writes
        # the SAME warm rows for both "date" and "popular" of a query.
        # Routing every chip's typed query through it collapsed ALL sort
        # tabs (Popular Now / New Uploads / Popular Week / Popular) into
        # the identical date-ordered page. hf is therefore only legitimate
        # when the REQUESTED sort actually maps to "date" — every other
        # sort goes straight to _direct_nhentai_search, which honours the
        # sort and reads/writes the per-sort Turso key
        # (search:q=<q>|sort=<s>|page=<N>) that BOT 1 warms.
        _sort_map_top = {"popular": "popular", "popular-week": "popular-week",
                         "popular-today": "popular-today", "date": "date",
                         "recent": "date"}
        _requested_sort = _sort_map_top.get((sort or "").lower(), "popular")
        if q_clean and HAVE_HF and hasattr(_hf, "search") and _requested_sort == "date":
            try:
                page_obj = _run_async(_hf.search(query=q_clean, page=upstream_page))
                if page_obj is not None:
                    hits = getattr(page_obj, "hits", None) or []
                    rows = [_hit_to_dict(h) for h in hits]
            except Exception as e:  # noqa: BLE001
                log.exception("hf_scraper.search failed for q=%r page=%s: %s",
                              q_clean, upstream_page, e)
                rows = []

        # v12.10 (#4): when the hf path (or an hf failure) yielded nothing
        # but the hf path was actually ATTEMPTED (non-empty q + hf available),
        # check the upstream 429 back-off cache for this (q, page) BEFORE
        # calling _direct_nhentai_search — otherwise the direct call's early
        # `return []` (ban live) is indistinguishable from a hard-empty page
        # and has_more turns False, hiding the Next-Page button on EVERY
        # sort tab during a rate-limit storm. _direct_nhentai_soft_empty
        # only probes the exact cache key the direct search uses, which the
        # hf branch never populates — hence this extra probe.
        if not rows and q_clean and HAVE_HF and hasattr(_hf, "search"):
            try:
                _sort_map = {"popular": "popular", "popular-week": "popular-week",
                             "popular-today": "popular-today", "date": "date",
                             "recent": "date"}
                _rs = _sort_map.get((sort or "").lower(), "popular")
                _ck = ("search", (" ".join(q_clean.lower().split()) if q_clean else "english"),
                       _rs, int(upstream_page))
                if _RATE_LIMIT_CACHE.get(_ck, 0) > _time.time():
                    rate_limited_pages.append(upstream_page)
            except Exception:  # noqa: BLE001
                pass

        if not rows:
            rows = _direct_nhentai_search(q_clean, upstream_page, sort or "popular")

        rows = _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max)

        if not rows:
            # v12.1 (B): distinguish 429-backoff empty from real end-of-results.
            if _direct_nhentai_soft_empty(q_clean, upstream_page, sort or "popular"):
                rate_limited_pages.append(upstream_page)
                consecutive_empty += 1
            else:
                # A hard-empty upstream page still might not be the true end
                # (English filter can zero-out a page). Only bail after a
                # small run of them.
                consecutive_empty += 1
            if consecutive_empty >= _MAX_CONSECUTIVE_SOFT_EMPTY:
                break
            upstream_page += 1
            continue

        consecutive_empty = 0
        collected.extend(rows)
        upstream_page += 1

    # v12.30 (DUP FIX): dedup by gallery id BEFORE slicing the window.
    # nhentai reorders popular lists constantly, so upstream pages 1 and 2
    # of the same sort routinely share several galleries. Left in place,
    # those duplicates show up as the SAME card twice on one user-facing
    # page (visible in the mini-app screenshots: identical covers repeated
    # on Discover). Preserves the original order the aggregator produced,
    # skips only exact-id repeats.
    _seen_ids: set[str] = set()
    _deduped: list[dict] = []
    for _row in collected:
        try:
            _rid = str(_row.get("id") or "").strip()
        except AttributeError:
            _rid = ""
        if not _rid:
            _deduped.append(_row)   # no id -> can't dedup, keep it
            continue
        if _rid in _seen_ids:
            continue
        _seen_ids.add(_rid)
        _deduped.append(_row)
    collected = _deduped

    window = collected[start_offset:start_offset + per_page]
    items = [_normalize(r) for r in window]

    # v1.22.6: honest has_more via a cache lookahead probe.
    # Previously the loop stopped the moment `collected` reached the
    # window size (e.g. page 2 = 50 cards from upstream pages 1+2), so
    # the cushion check `len(collected) > start_offset+per_page` was
    # ALWAYS false and the Mini App grayed the › button after page 2
    # even when Turso already held pages 3–30 warm. Now we probe the
    # NEXT upstream cache key directly: if it exists (fresh OR stale under
    # USE_OLD_CACHE), we know more pages exist. Cheap: one Turso GET.
    _lookahead_has_more = False
    try:
        # v1.22.7: probe whenever the window is non-empty. v1.22.6 required
        # exactly per_page items, but the post-dedup window can be short on
        # volatile sorts — which was precisely when › went gray.
        if items and q_clean in ("", "english", "language:english"):
            _nhc = _sb_turso_cache()
            if _nhc is not None:
                _next_key = f"search:{(sort or 'popular').strip().lower()}:page{upstream_page}"
                _allow_stale = os.environ.get("USE_OLD_CACHE", "1").strip() not in (
                    "0", "false", "no")
                _peek = _nhc.get(_next_key, allow_stale=_allow_stale)
                if isinstance(_peek, list) and _peek:
                    _lookahead_has_more = True
    except Exception:  # noqa: BLE001
        pass

    if not _return_meta:
        return items
    return {
        "items": items,
        # has_more: we EITHER filled the window AND some upstream cushion
        # remains, OR we didn't fill it but had to give up early due to
        # 429s (client can retry), OR the next-page cache probe found
        # more (the v1.22.6 lookahead fix). Any signal produces a truthful button.
        "has_more": (
            len(collected) > start_offset + per_page
            or _lookahead_has_more
            or bool(rate_limited_pages)
            or (upstream_page > max_upstream and len(items) == per_page)
        ),
        "upstream_pages_scanned": upstream_page - 1,
        "upstream_rate_limited_pages": rate_limited_pages,
    }


def _detail_rate_limited(gallery_id: str) -> bool:
    """v11.9: True when this gallery_id is currently inside the 429 backoff
    window. Used by the route to return 503 + Retry-After instead of the
    misleading 404 that left the frontend stuck on 'Loading details…'."""
    cache_key = ("detail", str(gallery_id))
    now = _time.time()
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    return bool(ban and ban > now)


def _detail_rate_limit_wait_sec(gallery_id: str) -> int:
    """Seconds until the current backoff expires (0 if not rate-limited)."""
    cache_key = ("detail", str(gallery_id))
    now = _time.time()
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    if not ban or ban <= now:
        return 0
    return max(1, int(ban - now))



def _pages_meta_of(d: dict) -> list:
    """v12.66: extract [{n, path, w, h}] — accepts v12.66+ rows
    (pages_meta), raw v2 payloads (images.pages / pages)."""
    src = None
    if isinstance(d, dict):
        if isinstance(d.get("pages_meta"), list): src = d["pages_meta"]
        elif isinstance(d.get("images"), dict) and isinstance(d["images"].get("pages"), list):
            src = d["images"]["pages"]
        elif isinstance(d.get("pages"), list): src = d["pages"]
    out = []
    for i, pp in enumerate(src or []):
        if not isinstance(pp, dict): continue
        out.append({"n": int(pp.get("number") or pp.get("n") or i + 1),
                    "path": str(pp.get("path") or ""),
                    "w": int(pp.get("width") or pp.get("w") or 0),
                    "h": int(pp.get("height") or pp.get("h") or 0)})
    return out



def _pages_meta_backfill(gid: str, row: dict) -> dict:
    """v12.68: cached gallery rows written before the Read viewer have no
    pages_meta. On first open of such a row, fetch the nhentai v2 detail
    ONCE (~400ms), build pages_meta from raw images.pages + media_id, and
    persist the enriched row. Best-effort: any failure returns row unchanged.

    v12.67 BUG FIX: the previous version called `_hf.fetch_detail(gid)` —
    a function that DOES NOT EXIST in hf_scraper.py (it only exposes the
    ASYNC fetch_gallery_meta, whose GalleryMeta carries no images). Every
    backfill raised AttributeError, was swallowed, and returned the row
    unchanged — so NO legacy row ever got pages_meta and Read showed
    'Reader unavailable' forever. We now do the upstream fetch ourselves
    (direct httpx to /api/v2/galleries/<id>) and write through
    nhentai_cache.put, which DUAL-WRITES Turso + Mongo-2 (dedup-guarded).
    """
    try:
        if row.get("pages_meta"):
            return row
    except Exception:
        return row
    # --- one upstream fetch (respects the shared 429 backoff budget) -------
    # v12.69: use _nh_headers() so NHENTAI_API_KEY (when set) is sent.
    # Anonymous v2 responses omit media_id/images.pages entirely, which is
    # exactly what v12.68 mis-diagnosed as "gallery removed upstream".
    try:
        r = httpx.get(
            f"{_NH_API}/galleries/{gid}",
            headers=_nh_headers(),
            timeout=15,
        )
        if r.status_code != 200:
            log.info("pages_meta backfill: upstream HTTP %s for %s",
                     r.status_code, gid)
            return row
        item = r.json() or {}
    except Exception as e:  # noqa: BLE001
        log.info("pages_meta backfill: fetch failed for %s (%s)", gid, e)
        return row
    # --- build pages_meta + persist -----------------------------------------
    try:
        meta = _pages_meta_from_raw(item)
        if not meta:
            log.info("pages_meta backfill: no images for %s (media_id/pages missing)", gid)
            return row
        row["pages_meta"] = meta
        if not row.get("pages") and not row.get("num_pages"):
            row["pages"] = int(item.get("num_pages") or len(meta))
        try:
            from . import nhentai_cache as _nhc2  # noqa: WPS433
            # Pass the DICT (not a json.dumps string) so put's canonical
            # gate + JSON-serialisation guard work correctly; put DUAL-WRITES
            # Turso AND Mongo-2 best-effort.
            _ok = _nhc2.put(f"gallery:{gid}", row, ttl_sec=None)
            log.info("📖 [PAGES BACKFILL] gid=%s pages=%d cached for Read (put=%s)",
                     gid, len(meta), _ok)
        except Exception as e:  # noqa: BLE001
            log.info("pages_meta backfill: persist failed for %s (%s)", gid, e)
    except Exception as e:  # noqa: BLE001
        log.info("pages_meta backfill: build failed for %s (%s)", gid, e)
    return row


def _gallery_detail_impl(gallery_id: str) -> dict:
    """Return the full detail dict for one gallery.

    Strategy: ALWAYS call the direct nhentai v2 endpoint for the rich
    fields. If that succeeds, return it. Only fall back to hf_scraper when
    the direct call fails (network/rate-limit), so the sheet at least gets
    a title + cover instead of nothing.
    """
    try:
        direct = _direct_nhentai_detail(str(gallery_id))
    except Exception as e:  # noqa: BLE001
        log.exception("_direct_nhentai_detail failed for %s: %s",
                      gallery_id, e)
        direct = {}

    if direct and direct.get("id"):
        # Provide both `tag_groups` (backend-preferred key) and `groups`
        # (what detail-sheet.js reads) so both frontends stay happy.
        if "tag_groups" in direct and "groups" not in direct:
            groups_by_type = {}
            for typ, names in (direct.get("tag_groups") or {}).items():
                groups_by_type[typ] = [{"name": n} for n in names]
            direct["groups"] = groups_by_type
        return direct

    # Fallback: hf_scraper (returns only id/title/cover/pages/tags).
    if HAVE_HF and hasattr(_hf, "fetch_gallery_meta"):
        try:
            meta = _run_async(_hf.fetch_gallery_meta(str(gallery_id)))
            if meta is not None:
                d = _meta_to_dict(meta)
                if d.get("id"):
                    # Best-effort synthesis of `groups` from the flat
                    # typed tags so the detail sheet still renders labelled
                    # rows in the fallback path.
                    groups: dict = {}
                    for t in (d.get("tags") or []):
                        typ = str(t.get("type") or "tag")
                        nm = str(t.get("name") or "")
                        if nm:
                            groups.setdefault(typ, []).append({"name": nm})
                    if groups:
                        d["groups"] = groups
                    return d
        except Exception as e:  # noqa: BLE001
            log.exception("hf_scraper.fetch_gallery_meta failed for %s: %s",
                          gallery_id, e)
    return {}


# ---------------------------------------------------------------------------
# v12.34i: gallery_suggestions(gid, limit) — "Similar to this" backend.
#
# nhentai's /api/v2/galleries/<id>?include=suggestions embeds the SAME
# structure a search hit returns, so we can normalise each entry with the
# same title/cover/tags builders used by _direct_nhentai_search + _direct_
# nhentai_detail and hand the frontend a list ready for the existing card
# component.
#
# Cache: `suggest:<gid>` key, TTL_SUGGEST_SEC (3 d by default; 0 when
# NHCACHE_NEVER_EXPIRE_ALL=1). One 429 back-off cache shared with search.
# Fail-open on every branch so the detail sheet never blocks on an error.
# ---------------------------------------------------------------------------
def gallery_suggestions(gallery_id: str, limit: int = 6) -> list[dict]:
    """v12.50: SQL-scored "Similar to this" — Turso-only, flat RAM.

    The v12.34k implementation SELECTed 2,000 full payloads and scored in
    Python (the mem_hrana_response pattern), and pre-v12.46 it silently
    returned [] through the broken libsql driver — the two reasons the
    frontend row never rendered. This version runs the weighted scoring
    (+10 artist/parody/group/character, +2 content tags) entirely inside
    Turso via json1; Python only ever sees <= `limit` card-sized rows.

    Response contract unchanged: list of card dicts {id, title, cover,
    pages, favorites, tags} (route adds is_cached), cached under
    similar:<gid>. Fail-open: any error -> [].
    """
    gid = str(gallery_id or "").strip()
    if not gid.isdigit():
        return []
    limit = max(1, min(int(limit or 6), 12))

    # v12.56: `similar:<gid>` Turso cache DISABLED.
    # Rationale:
    #   * SQL scoring in Turso already answers in ~1.2-1.5s.
    #   * Caching would burn ~500K writes/month (one per gallery) for
    #     data that goes stale as the Turso library grows.
    #   * Users get fresh, up-to-date recommendations every open.
    #   * The engine still uses gallery:<gid> for its target signals below,
    #     so Turso remains the single source of truth for cold data.
    _nhc = _sb_turso_cache()
    if _nhc is None:
        return []
    try:
        target = _nhc.get(f"gallery:{gid}", allow_stale=True)
    except Exception as e:  # noqa: BLE001
        log.warning("nhc.get(gallery:%s) raised: %s", gid, e)
        return []
    if not isinstance(target, dict) or not target.get("id"):
        # First-ever open of a cold gallery — it caches on this request,
        # the next open gets the row. Fail-open empty as before.
        log.info("similar(%s): target not yet in Turso; skipping", gid)
        return []

    try:
        from . import similar_sql as _ss      # noqa: WPS433
        from . import turso_client as _turso  # noqa: WPS433
    except Exception as e:  # noqa: BLE001
        log.warning("similar(%s): similar_sql/turso import failed: %s", gid, e)
        return []

    def _run(sql, args) -> list:
        if not sql:
            return []
        try:
            rs = _turso.execute(sql, list(args))
        except Exception as e:  # noqa: BLE001
            log.warning("similar(%s): stage query raised: %s", gid, e)
            return []
        if rs is None or not getattr(rs, "rows", None):
            return []
        cols = ["id", "title", "cover", "pages", "favorites", "score"]
        out = []
        for row in rs.rows:
            try:
                out.append(dict(zip(cols, list(row))))
            except Exception:  # noqa: BLE001
                continue
        return out

    sig = _ss._signals(target)
    out_rows: list = []
    seen: set = set()

    def _collect(rows) -> None:
        for r in rows:
            c = _ss.card_from_row(r)
            if c and c["id"] not in seen:
                seen.add(c["id"])
                out_rows.append(c)

    # ---- Stage A: high-tier prefilter + weighted score -------------------
    sqlA, argsA = _ss.build_stage_a(gid, sig, limit)
    rowsA = _run(sqlA, argsA)
    _collect(rowsA)
    stage_used = "A"

    # ---- Stage B: content-tag prefilter (deeper net) ----------------------
    if len(out_rows) < _ss.MIN_RESULTS:
        sqlB, argsB = _ss.build_stage_b(gid, sig, list(seen),
                                        limit - len(out_rows))
        _collect(_run(sqlB, argsB))
        if out_rows:
            stage_used = "A+B"

    # ---- Stage C: same artist/category by favorites (never-empty guard) ---
    if len(out_rows) < _ss.MIN_RESULTS:
        sqlC, argsC = _ss.build_stage_c(gid, target, list(seen),
                                        limit - len(out_rows))
        _collect(_run(sqlC, argsC))
        stage_used = stage_used + "+C"

    log.info("similar(%s): stage=%s returned=%d", gid, stage_used,
             len(out_rows))

    # v12.56: no cache write — see rationale above.
    return out_rows[:limit]


def route_status() -> dict:
    """Diagnostics for /api/admin/diag."""
    info: dict[str, Any] = {"have_hf": HAVE_HF}
    if HAVE_HF and hasattr(_hf, "route_status"):
        try:
            info["hf_route_status"] = _hf.route_status()
        except Exception as e:  # noqa: BLE001
            info["hf_route_status_error"] = str(e)
    if HAVE_HF and hasattr(_hf, "health_check"):
        try:
            info["hf_health_check"] = bool(_run_async(_hf.health_check()))
        except Exception as e:  # noqa: BLE001
            info["hf_health_check_error"] = str(e)
    else:
        info["source"] = "fallback nhentai"
    return info


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _normalize(row: dict) -> dict:
    if not row:
        return {}
    return {
        "id":    row.get("id") or row.get("gallery_id"),
        "title": row.get("title") or row.get("english_title") or "",
        "cover": row.get("cover") or row.get("cover_url") or row.get("thumb_url") or "",
        "pages": row.get("pages") or row.get("num_pages"),
        # v12.66: reader page metadata
        "pages_meta": _pages_meta_of(row),
        "tags":  row.get("tags") or [],
    }


def _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max):
    def _pass(r):
        tag_names = set()
        for t in (r.get("tags") or []):
            if isinstance(t, dict):
                name = (t.get("name") or "").lower()
            else:
                name = str(t).lower()
            if name:
                tag_names.add(name)
        if include_tags and not all(t in tag_names for t in include_tags):
            return False
        if exclude_tags and any(t in tag_names for t in exclude_tags):
            return False
        p = int(r.get("pages") or r.get("num_pages") or 0)
        if pages_min is not None and p < pages_min: return False
        if pages_max is not None and p > pages_max: return False
        return True
    return [r for r in rows if _pass(r)]


def gallery_detail(gid: str):
    """v12.67: backfill pages_meta for legacy cached rows before returning."""
    detail = _gallery_detail_impl(gid)
    try:
        if detail and detail.get("id") and not detail.get("pages_meta"):
            detail = _pages_meta_backfill(str(detail["id"]), detail)
    except Exception:
        pass
    return detail
