"""
hf_scraper.py — Direct scraper for nhentai.net (formerly hentaifox.com).

WHY THIS MODULE CHANGED SOURCE (2026-08-01 — nhentai switch)
------------------------------------------------------------
hentaifox.com sits behind Cloudflare Turnstile and hard-blocks Render's
datacenter IP range. We verified inside the Render container itself:
    HTTP 403 · server: cloudflare · cf-mitigated: challenge · 0 galleries

nhentai.net returns HTTP 200 to a plain httpx GET from the same IP and,
better still, exposes a clean JSON API that its own SvelteKit frontend
uses. Two endpoints do the whole job:

    GET /api/v2/search?query=<q>&sort=date&page=<n>
        → list of galleries with english_title / japanese_title, num_pages,
          media_id, and a thumbnail path.

    GET /api/v2/galleries/<id>?include=related,suggestions,comments
        → full detail incl. title.pretty (clean, no artist/language brackets),
          resolved tags with names+types, cover path.

Because nhentai and hentaifox use different numeric ID spaces, gallery
URLs handed to Bot 1 (@postedstuffbot) and Bot 2 (@Gallery_DLBot) are now
in the form:
        https://nhentai.net/g/<id>/
Both bots accept this format (confirmed by user).

TITLE STRATEGY (as requested)
-----------------------------
Two-stage titles:

  1. SEARCH RESULTS (the picker with rows on each page):
     Use `english_title` if present, else `japanese_title`. Fast — one API
     call returns all rows. Users see the picker in ~300 ms.

  2. CONFIRMED / QUEUED ITEMS (progress messages, batch labels):
     After the user hits Confirm, `fetch_gallery_meta()` is called for
     each selected gallery. That endpoint returns the clean `pretty` title
     which is what shows up in the progress tracker + final "posted" line.

     This keeps /search snappy while giving humans a clean title in the
     places they actually read.

CACHING & DEDUP
---------------
  * response cache — 90 s for search JSON, 30 min for gallery detail JSON
  * in-flight dedup — two callers asking for the same URL simultaneously
    share ONE upstream request
  * cache is bounded (128 entries max) with an LRU-ish trim

PUBLIC API (unchanged from previous version)
-------------------------------------------
Everything downstream (search_picker.py, relay.py, worker.py, admin_bot.py)
keeps working without edits:

    async search(query, page=1) -> Optional[SearchPage]
    async fetch_gallery_meta(url_or_id) -> Optional[GalleryMeta]
    async health_check() -> bool
    route_status() -> dict            (used by /diag)

NO env vars required. No proxies. No third-party services.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx

from logging_setup import setup_logging

log = setup_logging("hf_scraper")

# ---------------------------------------------------------------------------
# Site constants
# ---------------------------------------------------------------------------
BASE_URL = "https://nhentai.net"
API_URL = f"{BASE_URL}/api/v2"

# v12.2: nhentai.net/api/v2/openapi.json explicitly asks for a DESCRIPTIVE
# User-Agent — quote: "Please set a descriptive User-Agent header:
# AppName/version (contact or project URL). This helps us identify traffic
# and reach out if needed." We honour that instead of masquerading as Chrome
# so their WAF stops classifying us as a generic scraper. Env-overridable
# so ops can bump the version or swap the contact URL without a redeploy.
_UA_DEFAULT = "DoujinshiUniverse/12.3 (+https://github.com/vairegi/mtproto-userbot)"
_HEADERS = {
    "User-Agent": os.environ.get("NHENTAI_USER_AGENT", _UA_DEFAULT),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # No 'br' — we don't ship the brotli package, so gzip/deflate is the
    # safe, universal choice. nhentai serves fine over gzip.
    "Accept-Encoding": "gzip, deflate",
    "Referer": f"{BASE_URL}/",
}
# v12.2: optional API key from nhentai.net/user/settings#apikeys. When
# present, doubles/triples anon quotas per the openapi.json spec.
_NHENTAI_API_KEY = os.environ.get("NHENTAI_API_KEY", "").strip()
if _NHENTAI_API_KEY:
    _HEADERS["Authorization"] = f"Key {_NHENTAI_API_KEY}"

_TIMEOUT = 20.0
_SEARCH_CACHE_TTL_SEC = 90
_GALLERY_CACHE_TTL_SEC = 30 * 60
_CACHE_MAX_ENTRIES = 128

# v12.8: emoji-tagged log lines so the operator can grep Render logs
# for one glance-friendly signal per cache event.
_LOG_HIT    = "⚡ [TURSO CACHE HIT] Served data from Turso  key=%s  ttl=%ss"
_LOG_STALE  = "⚠ [TURSO STALE HIT] Served STALE data (upstream 429/down)  key=%s"
_LOG_MISS   = "🌐 [CACHE MISS] Fetched from upstream API and cached to Turso  key=%s  ttl=%ss"
_LOG_WRITE  = "📝 [TURSO WRITE] Uploaded payload to Turso  key=%s  ttl=%ss  bytes=%s"
_LOG_LOCAL  = "💾 [LOCAL CACHE HIT] Served from in-process dict (sub-90s window)  key=%s"
_LOG_BUCKET = "🚫 [BUCKET BLOCK] anon quota exhausted for %s — refusing upstream call"


# ---------------------------------------------------------------------------
# Response cache + in-flight dedup
# ---------------------------------------------------------------------------
_cache: Dict[str, Tuple[float, Any]] = {}     # key -> (expires_at, value)
_cache_lock = threading.Lock()
_inflight: Dict[str, "asyncio.Future[Optional[Any]]"] = {}
_inflight_lock = threading.Lock()


def _cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.time() > expires_at:
            _cache.pop(key, None)
            return None
        return value


def _cache_put(key: str, value: Any, ttl_sec: int) -> None:
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX_ENTRIES:
            # LRU-ish trim: drop the oldest quarter by expiry.
            for k in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX_ENTRIES // 4]:
                _cache.pop(k, None)
        _cache[key] = (time.time() + ttl_sec, value)


# ---------------------------------------------------------------------------
# Data models — public API surface (unchanged shape, drop-in compatible)
# ---------------------------------------------------------------------------
@dataclass
class SearchHit:
    gallery_id: str
    title: str
    url: str
    thumb_url: Optional[str] = None
    category: Optional[str] = None    # kept in dataclass for compat; not set from search


@dataclass
class SearchPage:
    query: str
    page: int
    total_results: int
    hits: List[SearchHit] = field(default_factory=list)
    has_next: bool = False


@dataclass
class GalleryMeta:
    # BUG FIX: `tags` is now List[Dict[str,str]] with {'name','type'} so
    # downstream (cover_poster) can build grouped meta rows.
    # Legacy consumers can call hf_scraper._flatten_tag_names(meta.tags).
    title: str
    tags: List[Dict[str, str]]
    cover_url: Optional[str]
    pages: Optional[int] = None
    gallery_id: Optional[str] = None
    # v11: page-1 image URL. nhentai's `cover.path` is a downscaled
    # thumbnail (`cover.jpg.webp`); page 1 is the high-quality equivalent
    # served from i.nhentai.net at full resolution. Falls back to
    # `cover_url` when the detail response is missing `media_id`/`images`.
    page1_url: Optional[str] = None


# ---------------------------------------------------------------------------
# HTTP layer — one shared AsyncClient per event loop, plain httpx GETs to nhentai
# ---------------------------------------------------------------------------
#
# v11.2 bug fix: previously a single process-wide AsyncClient was created
# on first use and reused forever. Uvicorn's reload mode and the start.sh
# relay-restart cycle both create a fresh event loop for the next worker,
# but the old AsyncClient (and the asyncio.Event primitives inside
# httpx's connection-pool limits) stayed bound to the DEAD loop. First
# request into the new loop then blew up with:
#
#   nhentai request failed for /search: <asyncio.locks.Event object at
#   0x... [unset]> is bound to a different event loop
#
# Fix: key the client by the currently-running event loop. When a loop is
# torn down its entry in the dict becomes unreachable and gets garbage
# collected on the next tick.
_client_lock = threading.Lock()
# v11.2: keyed by the loop object itself (NOT id(loop)) — CPython may
# reuse the same id() for a new loop after the previous one is
# garbage-collected, which would silently hand a closed-loop client to
# the new loop and re-trigger the exact bug we're fixing. Loop objects
# are hashable and identity-stable.
_clients_by_loop: "dict[asyncio.AbstractEventLoop, httpx.AsyncClient]" = {}


async def _get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient for the CURRENT event loop.

    Stale-closed-loop entries are evicted lazily on first call in the
    new loop so the dict stays bounded even under heavy uvicorn reloads.
    """
    loop = asyncio.get_running_loop()
    existing = _clients_by_loop.get(loop)
    if existing is not None:
        return existing
    with _client_lock:
        # Drop entries whose loop has been closed — they can never be
        # used again and would otherwise accumulate forever.
        stale = [lp for lp in _clients_by_loop if lp.is_closed()]
        for lp in stale:
            _clients_by_loop.pop(lp, None)

        existing = _clients_by_loop.get(loop)
        if existing is None:
            existing = httpx.AsyncClient(
                timeout=_TIMEOUT,
                follow_redirects=True,
                headers=_HEADERS,
                http2=False,   # nhentai serves fine over http/1.1
            )
            _clients_by_loop[loop] = existing
    return existing


# Back-compat shim: some legacy code paths read the raw `_client` global.
# Keep the symbol defined as None so any such reader hits its own
# None-guard instead of NameError.
_client: Optional[httpx.AsyncClient] = None


# v11.2 / v11.6: short per-path back-off cache for 429s. When nhentai
# rate-limits us, subsequent identical paths hit this cache instead of
# hammering upstream and flooding the log. Keyed by (path, frozenset(params)).
#
# v11.6 hardening:
#   * Base TTL raised 30s -> 60s (empirically nhentai's window is closer
#     to a full minute; 30s often just re-hit the ban).
#   * Exponential ramp on repeat 429s for the same key (cap 120s).  # v12.54
#   * Honour the server's `Retry-After` header when present.
#   * All three tunables env-overridable so ops can adjust without redeploy:
#         NH_RATE_LIMIT_TTL_SEC        (default 20  -- v12.54)
#         NH_RATE_LIMIT_TTL_CAP_SEC    (default 120 -- v12.54)
#         NH_RATE_LIMIT_RAMP           (default 2.0)
import os as _os_rl  # local alias, avoid collision with top-level `os`
_RATE_LIMIT_CACHE: "dict[tuple, float]" = {}
_RATE_LIMIT_STRIKES: "dict[tuple, int]" = {}
try:
    _RATE_LIMIT_TTL_SEC = int(_os_rl.environ.get("NH_RATE_LIMIT_TTL_SEC", "20"))
except (TypeError, ValueError):
    _RATE_LIMIT_TTL_SEC = 20
try:
    _RATE_LIMIT_TTL_CAP_SEC = int(_os_rl.environ.get("NH_RATE_LIMIT_TTL_CAP_SEC", "120"))
except (TypeError, ValueError):
    _RATE_LIMIT_TTL_CAP_SEC = 120
try:
    _RATE_LIMIT_RAMP = float(_os_rl.environ.get("NH_RATE_LIMIT_RAMP", "2.0"))
except (TypeError, ValueError):
    _RATE_LIMIT_RAMP = 2.0


def _rate_limit_backoff_sec(cache_key: tuple, retry_after: Optional[str]) -> int:
    """Compute the next back-off duration for a rate-limited key.

    - If the upstream sent Retry-After (seconds), respect it (clamped to cap).
    - Otherwise, TTL * ramp^strikes, clamped to [TTL, TTL_CAP].
    Strike count is stored per key and reset only when the key succeeds.
    """
    if retry_after:
        try:
            ra = int(float(str(retry_after).strip()))
            return max(_RATE_LIMIT_TTL_SEC, min(_RATE_LIMIT_TTL_CAP_SEC, ra))
        except (TypeError, ValueError):
            pass
    strikes = _RATE_LIMIT_STRIKES.get(cache_key, 0)
    dur = _RATE_LIMIT_TTL_SEC * (_RATE_LIMIT_RAMP ** strikes)
    _RATE_LIMIT_STRIKES[cache_key] = strikes + 1
    return int(max(_RATE_LIMIT_TTL_SEC, min(_RATE_LIMIT_TTL_CAP_SEC, dur)))


# v12.2: identify which openapi.json bucket a path consumes from. Kept
# tiny so the mapping is trivial to audit and edit.
def _bucket_id_for_path(path: str) -> str:
    p = path or ""
    if p.startswith("/search"):                 return "search"
    if p.startswith("/galleries/popular"):      return "popular"
    if "/suggestions" in p:                     return "suggestions"
    if p.startswith("/galleries/") or p.startswith("/gallery/"):
        return "galleries"
    if p.startswith("/galleries"):              return "galleries_list"
    return "galleries_list"


async def _http_get_json(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """
    GET a nhentai JSON endpoint and return its parsed body, or None on failure.
    Never raises upward — callers get None and can surface a clean UX error.

    v11.2 changes:
      - 429s are soft-failed with a single WARNING (no stack trace) and a
        30s per-key back-off so the log stops flooding when the upstream
        rate-limits us.
      - Generic exceptions are also logged at WARNING without a traceback
        so transient network failures don't drown out real errors.

    v12.2 changes:
      - Consumes a token from the SHARED per-endpoint bucket in Mongo
        BEFORE firing upstream. When the bucket is dry (any user's search
        can drain it), we fail closed here instead of hammering nhentai
        and getting the whole IP banned for everyone.
    """
    url = f"{API_URL}{path}"
    cache_key = (path, frozenset((params or {}).items()))
    now = time.time()
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    if ban and ban > now:
        # Still inside the back-off window — soft-fail silently.
        return None
    # v12.2: shared token-bucket gate (sized to openapi.json anon limits).
    # Fails OPEN if the cache service can't reach Mongo, so dev/test still work.
    try:
        from miniapp.backend.app.services import nhentai_cache as _nhc
        if not _nhc.try_consume(_bucket_id_for_path(path)):
            log.warning(_LOG_BUCKET, path)
            return None
    except Exception:  # noqa: BLE001
        # Cache module import failure: fail open, log once at DEBUG so the
        # log stays quiet. The bucket is defence-in-depth on top of the
        # existing _RATE_LIMIT_CACHE back-off, not the only gate.
        pass
    try:
        client = await _get_client()
        r = await client.get(url, params=params)
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After") if hasattr(r, "headers") else None
            dur = _rate_limit_backoff_sec(cache_key, retry_after)
            _RATE_LIMIT_CACHE[cache_key] = now + dur
            log.warning(
                "nhentai HTTP 429 for %s params=%s — backing off for %ss"
                "%s",
                path, params, dur,
                f" (Retry-After={retry_after})" if retry_after else "",
            )
            return None
        # Success path: reset the strike counter for this key so a later
        # 429 for the same key starts back at the base TTL.
        if 200 <= r.status_code < 300:
            _RATE_LIMIT_STRIKES.pop(cache_key, None)
        if r.status_code != 200:
            log.warning("nhentai HTTP %s for %s params=%s", r.status_code, path, params)
            return None
        try:
            return r.json()
        except json.JSONDecodeError as e:
            log.warning("nhentai returned non-JSON (%s) for %s: %s",
                        e, path, r.text[:120])
            return None
    except RuntimeError as e:
        # v11.2: catch the asyncio event-loop binding error explicitly so
        # it's a single-line warning, not an opaque traceback. The
        # per-loop _get_client above already fixed the root cause; this
        # is belt-and-braces for any third-party path that bypasses it.
        if "event loop" in str(e).lower() or "different loop" in str(e).lower():
            log.warning(
                "nhentai request failed (event-loop binding) for %s: %s "
                "— likely a stale AsyncClient; will recover on next call",
                path, e,
            )
            return None
        log.warning("nhentai request failed for %s: %s", path, e)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("nhentai request failed for %s: %s", path, e)
        return None


def _turso_key_for(cache_key: str) -> str:
    """Map an hf_scraper cache_key to the Turso key convention so
    nhentai_cache.ttl_for_key picks the right TTL:
        gallery|123        -> gallery:123          (30 days)
        search|q=xxx|...   -> search:<cache_key>   (3 days)
    """
    if cache_key.startswith("gallery|"):
        return "gallery:" + cache_key.split("|", 1)[1]
    if cache_key.startswith("search:"):
        # v12.55: typed-search keys (search:<q>:p<N>) map straight onto the
        # canonical Bot 0 key instead of the legacy search:search:* row —
        # one shared row, half the Turso reads, no new legacy rows.
        rest = cache_key[len("search:"):]
        head, sep, tail = rest.rpartition(":p")
        if sep and tail.isdigit():
            qn = " ".join(head.lower().split())
            return f"search:q={qn}|sort=date|page={int(tail)}"
    return "search:" + cache_key


def _turso_cache_module():
    """Lazy import of nhentai_cache; None if not importable (test envs)."""
    try:
        from miniapp.backend.app.services import nhentai_cache as _nhc
        return _nhc
    except Exception:  # noqa: BLE001
        return None


async def _fetch_json_cached(cache_key: str, path: str,
                             params: Optional[dict], ttl_sec: int) -> Optional[dict]:
    """
    JSON fetch with a THREE-TIER cache:
        L1 : in-process dict (_cache)      — sub-second, 90s TTL
        L2 : Turso (nhentai_cache)         — 3-day TTL for searches
        L3 : nhentai.net/api/v2            — actual upstream call

    Every hop that answers prints ONE emoji-tagged log line so Render
    logs are greppable. v12.8 adds L2 (Turso) — earlier this was only
    written to by prefetch_cron, never read at request time.
    """
    # ---- L1: local dict -----------------------------------------------
    cached = _cache_get(cache_key)
    if cached is not None:
        log.info(_LOG_LOCAL, cache_key)
        return cached

    # ---- L2: Turso ----------------------------------------------------
    nhc = _turso_cache_module()
    turso_key = _turso_key_for(cache_key)
    if nhc is not None:
        try:
            payload = nhc.get(turso_key, allow_stale=False)
        except Exception:  # noqa: BLE001
            payload = None
        if payload is not None:
            ttl_remain = getattr(nhc, "ttl_for_key", lambda _k: 0)(turso_key)
            log.info(_LOG_HIT, turso_key, ttl_remain)
            _cache_put(cache_key, payload, ttl_sec)   # re-warm L1
            return payload

    # ---- In-flight dedup ----------------------------------------------
    loop = asyncio.get_running_loop()
    with _inflight_lock:
        existing = _inflight.get(cache_key)
        if existing is not None:
            future = existing
            owner = False
        else:
            future = loop.create_future()
            _inflight[cache_key] = future
            owner = True

    if not owner:
        try:
            return await future
        except Exception:  # noqa: BLE001
            return None

    # ---- L3: upstream -------------------------------------------------
    try:
        data = await _http_get_json(path, params)

        if data is not None:
            try:
                payload_bytes = len(json.dumps(data, default=str))
            except Exception:  # noqa: BLE001
                payload_bytes = -1
            log.info(_LOG_MISS, turso_key, ttl_sec)
            _cache_put(cache_key, data, ttl_sec)                    # L1
            if nhc is not None and (
                    turso_key.startswith("gallery:")
                    or turso_key.startswith("search:q=")):
                # v12.55: write canonical keys ONLY — legacy shapes
                # (search:search:* etc.) must never be created again.
                try:
                    ok = bool(nhc.put(turso_key, data))             # L2 write
                    if ok:
                        applied_ttl = getattr(nhc, "ttl_for_key",
                                              lambda _k: ttl_sec)(turso_key)
                        log.info(_LOG_WRITE, turso_key, applied_ttl, payload_bytes)
                except Exception as e:  # noqa: BLE001
                    log.debug("turso write failed for %s: %s", turso_key, e)
        else:
            # Upstream failed (429/network) — serve stale from Turso.
            if nhc is not None:
                try:
                    stale = nhc.get(turso_key, allow_stale=True)
                except Exception:  # noqa: BLE001
                    stale = None
                if stale is not None:
                    log.info(_LOG_STALE, turso_key)
                    _cache_put(cache_key, stale, ttl_sec)
                    future.set_result(stale)
                    return stale

        future.set_result(data)
        return data
    except Exception as e:  # noqa: BLE001
        future.set_exception(e)
        return None
    finally:
        with _inflight_lock:
            _inflight.pop(cache_key, None)


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Local title cleaning — no extra API calls
# ---------------------------------------------------------------------------
# nhentai's raw `english_title` typically looks like:
#     [Artist (Group)] Real Title Here [English] [Digital]
# Their `pretty` title strips the artist prefix and metadata suffixes to yield
# just "Real Title Here". Fetching `pretty` from /api/v2/galleries/<id> for
# every search row triggers HTTP 429 rate-limits after ~20 requests (measured
# against the live API), so we reproduce the cleaning locally instead.
#
# Validated against 75 live titles (74/75 cleaned, 0 empty) + 9 edge cases:
#     [STORM HAMMER (RAMDAC 300)] Onee-chan ni Makasenasai! [English] [Digital]
#         ->  Onee-chan ni Makasenasai!
#     [Some Author] Taming my stepsister 1-15 [English]
#         ->  Taming my stepsister 1-15
#
# Rule of thumb: WHEN IN DOUBT, KEEP THE TEXT. Too-aggressive cleaning deletes
# real words; a too-conservative pass just leaves a slightly longer title.
# The wrong-direction failure is much worse.
# ---------------------------------------------------------------------------

# Tokens recognised as METADATA when they appear alone inside a bracket.
_METADATA_LOWER = {
    "english", "eng", "japanese", "jp", "chinese", "ch", "中国翻訳", "英訳",
    "russian", "korean", "kr", "spanish", "french", "portuguese", "pt-br",
    "italian", "german", "translated", "traduzido",
    "digital", "dl版", "dl", "scan", "scanned", "decensored", "uncensored",
    "censored", "colorized", "colored", "full color", "full colour",
    "reprint", "final", "complete", "ongoing", "wip",
}

# Leading brackets that ARE the title itself, not an artist name. Keep these.
_LEADING_KEEP_LOWER = {
    "anthology", "artbook", "artist cg", "artist cg set", "cg set", "cg",
    "game cg", "doujin cg", "pixiv", "twitter",
}

# Splitter for stacked metadata like "[English | Digital]" or "[Eng / DL]".
_INNER_SPLIT_RE = re.compile(r"[|/,·・;+]|\s+-\s+|\s{2,}")


def _is_metadata_bracket(inner: str) -> bool:
    """True if the text inside a []-bracket is only metadata tokens."""
    s = inner.strip()
    if not s:
        return True
    parts = [p.strip() for p in _INNER_SPLIT_RE.split(s) if p.strip()]
    return all(p.lower() in _METADATA_LOWER for p in (parts or [s]))


def clean_title(raw: str) -> str:
    """
    Trim [Artist] prefix + [Language]/[Digital]/etc. suffixes from a raw
    nhentai title so it reads cleanly in the search picker and captions.
    Never returns an empty string.
    """
    if not raw:
        return raw
    s = raw.strip()

    # Step 1 — strip ONE leading [Artist] / [Group (SubGroup)] bracket.
    m = re.match(r"^\[([^\[\]]*)\]\s*(.+)$", s)
    if m:
        inner = m.group(1).strip()
        rest = m.group(2).strip()
        if rest and len(rest) >= 3 and inner.lower() not in _LEADING_KEEP_LOWER:
            s = rest

    # Step 2 — strip trailing metadata brackets one at a time.
    while True:
        m = re.match(r"^(.*?)\s*\[([^\[\]]*)\]\s*$", s)
        if not m:
            break
        head, inner = m.group(1), m.group(2)
        if not _is_metadata_bracket(inner):
            break
        s = head.rstrip()

    # Step 3 — stray leading language-only bracket.
    m = re.match(r"^\[([^\[\]]*)\]\s*(.+)$", s)
    if m and _is_metadata_bracket(m.group(1)) and len(m.group(2)) >= 3:
        s = m.group(2).strip()

    # Step 4 — collapse whitespace + safety net.
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) < 3:
        return raw.strip()
    return s


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
def _pick_search_title(item: dict) -> str:
    """
    Title strategy for SEARCH ROWS: english_title -> japanese_title -> id,
    then clean_title() so the picker button shows just the human-readable
    title without [Artist] or [English]/[Digital] noise.
    """
    # v12.55: canonical card dicts carry title_en_clean/title, not
    # english_title — check all three so warm list rows keep their titles.
    en = (item.get("english_title") or item.get("title_en_clean")
          or item.get("title") or "").strip()
    if en:
        return clean_title(en)
    jp = (item.get("japanese_title") or "").strip()
    if jp:
        return clean_title(jp)
    return f"Gallery {item.get('id', '?')}"


def _pretty_title_from_detail(detail: dict) -> str:
    """
    Title strategy for CONFIRMED / QUEUED ITEMS: nhentai's own `pretty` field
    is the gold standard, so we use it directly when present. If they only
    give us english/japanese, run those through clean_title().
    """
    t = detail.get("title") or {}
    pretty = (t.get("pretty") or "").strip()
    if pretty:
        return pretty
    for k in ("english", "japanese"):
        v = (t.get(k) or "").strip()
        if v:
            return clean_title(v)
    return f"Gallery {detail.get('id', '?')}"



def _thumb_url(item_or_detail: dict) -> Optional[str]:
    """Best-effort thumbnail URL for a search-result row."""
    thumb = (item_or_detail.get("thumbnail") or "").strip()
    if thumb:
        # nhentai returns a bare path like "galleries/4085333/thumb.jpg.webp".
        # Their CDN host is t3/t4.nhentai.net; the site's own frontend uses t3.
        return f"https://t3.nhentai.net/{thumb}"
    return None


def _cover_url_from_detail(detail: dict) -> Optional[str]:
    cover = detail.get("cover") or {}
    path = (cover.get("path") or "").strip()
    if path:
        return f"https://t3.nhentai.net/{path}"
    # Some detail responses only have `thumbnail`.
    return _thumb_url(detail)


# v11: nhentai extension codes -> file extensions. This is the same table
# used by the site's own frontend to build https://i.nhentai.net/... URLs.
# 'j' = JPEG, 'p' = PNG, 'g' = GIF, 'w' = WebP.
_NH_EXT_MAP = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}


def _page1_url_from_detail(detail: dict) -> Optional[str]:
    """Build the high-quality page-1 image URL from an nhentai detail dict.

    Example: for gallery 146595 with media_id="614941" and images.pages[0]
    of shape {"t": "j", "w": ..., "h": ...}, this returns
    ``https://i.nhentai.net/galleries/614941/1.jpg`` — which is what the
    site itself serves under /g/146595/1/ and is significantly higher
    quality than the ``t.nhentai.net/.../cover.jpg.webp`` thumbnail.

    Returns None when the detail response is missing either `media_id`
    or a first page entry; callers should fall back to `_cover_url_from_detail`.
    """
    media_id = str(detail.get("media_id") or "").strip()
    images = detail.get("images") or {}
    pages = images.get("pages") if isinstance(images, dict) else None
    if not (media_id and isinstance(pages, list) and pages):
        return None
    first = pages[0] if isinstance(pages[0], dict) else {}
    ext_code = (first.get("t") or "j").strip().lower()
    ext = _NH_EXT_MAP.get(ext_code, "jpg")
    return f"https://i.nhentai.net/galleries/{media_id}/1.{ext}"


# nhentai's tag `type` field uses these exact strings. We keep ALL of them
# so the cover caption can build grouped meta rows
# (Groups / Parodies / Artists / Characters / Languages / Categories) and
# still have a trailing plain-tag row.
_KEEP_TAG_TYPES = {
    "tag", "artist", "parody", "character", "group", "language", "category",
}


def _tag_names_from_detail(detail: dict) -> List[Dict[str, str]]:
    """Extract useful tags as [{'name','type'}, ...].

    BUG FIX: previously returned plain `List[str]` (no type info) AND
    dropped `language`/`category` tags entirely. The cover caption's
    grouped rows were empty as a result. We now preserve the type and
    include every meaningful category nhentai emits.

    Back-compat: downstream code that expected a flat list of names can
    call `_flatten_tag_names(...)` on the result (a helper below).
    """
    out: List[Dict[str, str]] = []
    for t in detail.get("tags") or []:
        if not isinstance(t, dict):
            continue
        ttype = (t.get("type") or "").strip().lower()
        name = (t.get("name") or "").strip()
        if not name:
            continue
        if ttype in _KEEP_TAG_TYPES:
            out.append({"name": name, "type": ttype or "tag"})
    return out


def _flatten_tag_names(tags: List[Dict[str, str]]) -> List[str]:
    """Legacy helper: [{'name','type'}, ...] -> ['name', ...]."""
    return [t.get("name", "") for t in (tags or []) if t.get("name")]


# ---------------------------------------------------------------------------
# Public API — search()
# ---------------------------------------------------------------------------
async def search(query: str, page: int = 1) -> Optional[SearchPage]:
    """
    Scrape https://nhentai.net/api/v2/search?query=<query>&sort=date&page=<page>.

    Returns None on network/parse failure (caller should show
    "search unavailable" rather than crash). Returns an empty-hit page when
    the query has no results, so the caller can distinguish "unavailable"
    from "genuinely empty".
    """
    q = (query or "").strip()
    if not q:
        return None

    params: Dict[str, Any] = {"query": q, "sort": "date", "page": int(page or 1)}
    cache_key = f"search:{q}:p{page}"

    # v12.19 (cache-key alignment): BOT 1 (ScraperBot) warms the SHARED
    # Turso/Mongo cache under BOT 0's canonical user-search key format
    #     search:q=<lower>|sort=<s>|page=<N>
    # which this function's legacy `search:<q>:p<N>` ( -> Turso
    # `search:search:<q>:p<N>`) NEVER matches — so every typed search was
    # a permanent L3 upstream call. Read BOT 1's warm row FIRST (query
    # lowercased on BOTH sides); on miss fall through to the legacy
    # cached fetch, then dual-write the result under the canonical key
    # so the NEXT read of this query is a Turso HIT. Both "date" and
    # "popular" orderings of the same query are served from the same
    # underlying upstream page, so we read both warm variants.
    _qn = " ".join(q.lower().split())
    _nhc0 = _turso_cache_module()
    if _nhc0 is not None:
        for _srt in ("date",):  # v12.55: canonical sweeps write sort=date only
            _warm_key = f"search:q={_qn}|sort={_srt}|page={int(page or 1)}"
            try:
                _warm = _nhc0.get(_warm_key, allow_stale=False)
            except Exception:  # noqa: BLE001
                _warm = None
            if isinstance(_warm, (dict, list)):  # v12.55: canonical rows are lists
                _ttl_r = getattr(_nhc0, "ttl_for_key", lambda _k: 0)(_warm_key)
                log.info(_LOG_HIT, _warm_key, _ttl_r)
                _cache_put(cache_key, _warm, _SEARCH_CACHE_TTL_SEC)  # re-warm L1
                data = _warm
                break
        else:
            data = None
    else:
        data = None

    if data is None:
        data = await _fetch_json_cached(cache_key, "/search", params, _SEARCH_CACHE_TTL_SEC)
        if data is not None and _nhc0 is not None:
            # Dual-write the freshly-fetched payload under the canonical
            # key so subsequent typed searches (either bot) hit L2.
            _canon = f"search:q={_qn}|sort=date|page={int(page or 1)}"
            try:
                _nhc0.put(_canon, data)
            except Exception as e:  # noqa: BLE001
                log.debug("canonical turso write failed for %s: %s", _canon, e)
    if data is None:
        return None

    try:
        # v12.55: canonical Turso rows are a LIST of card dicts (Bot 1
        # writes them normalised); raw upstream JSON is a dict envelope.
        # Accepting the list kills the "'list' object has no attribute
        # 'get'" WARNING.
        is_card_list = isinstance(data, list)
        if is_card_list:
            results = data
            num_pages = 1
            per_page = len(results) or 25
            total = per_page
        else:
            results = data.get("result") or []
            num_pages = int(data.get("num_pages") or 1)
            per_page = int(data.get("per_page") or len(results) or 25)
            total = int(data.get("total") or (num_pages * per_page))

        # English-only filter: nhentai's language tag IDs are stable —
        #   12227 = english  (tag name verified from /api/v2/galleries/<id>)
        # A gallery is "English" iff 12227 is in its tag_ids list.
        # Users asked to see ONLY English content in the picker, so any row
        # without that tag ID is dropped here, before building SearchHit.
        _ENGLISH_TAG_ID = 12227

        hits: List[SearchHit] = []
        for item in results:
            gid = item.get("id")
            if gid is None:
                continue

            # English-only filter. Raw upstream rows carry tag_ids;
            # canonical card lists were already language-filtered by
            # Bot 1's sweeps, so the tag-ids check would drop them all.
            if not is_card_list:
                tag_ids = item.get("tag_ids") or []
                if _ENGLISH_TAG_ID not in tag_ids:
                    continue

            gid_str = str(gid)
            hits.append(
                SearchHit(
                    gallery_id=gid_str,
                    title=_pick_search_title(item),
                    url=f"{BASE_URL}/g/{gid_str}/",
                    thumb_url=_thumb_url(item),
                    category=None,
                )
            )


        return SearchPage(
            query=q,
            page=int(page or 1),
            total_results=total,
            hits=hits,
            has_next=(int(page or 1) < num_pages),
        )
    except Exception as e:  # noqa: BLE001
        log.warning("nhentai: failed to normalise search response: %s", e)
        return None


# ---------------------------------------------------------------------------
# Public API — fetch_gallery_meta()
# ---------------------------------------------------------------------------
_GALLERY_ID_RE = re.compile(r"/g/(\d+)")


def _extract_gallery_id(url_or_id: str) -> Optional[str]:
    """
    Accept:
      - a bare numeric id             ("668505")
      - a full nhentai gallery URL    ("https://nhentai.net/g/668505/")
      - a legacy hentaifox URL        (best-effort ID extraction)
    """
    s = (url_or_id or "").strip()
    if not s:
        return None
    if s.isdigit():
        return s
    m = _GALLERY_ID_RE.search(s)
    if m:
        return m.group(1)
    m2 = re.search(r"/gallery/(\d+)", s)  # legacy hentaifox form
    if m2:
        return m2.group(1)
    return None


async def fetch_gallery_meta(gallery_url_or_id: str) -> Optional[GalleryMeta]:
    """
    Fetch and normalise a nhentai gallery's metadata.

    Uses the /api/v2/galleries/<id> endpoint. Returns the *pretty* title
    (clean, no artist/language brackets) for use in progress messages and
    the final "posted" line.
    """
    gid = _extract_gallery_id(gallery_url_or_id)
    if not gid:
        return None

    cache_key = f"gallery:{gid}"
    data = await _fetch_json_cached(
        cache_key,
        f"/galleries/{gid}",
        {"include": "related,suggestions,comments"},
        _GALLERY_CACHE_TTL_SEC,
    )
    if data is None:
        return None

    try:
        # v11: prefer page 1 as the cover image (nhentai's `cover.path`
        # is a downscaled JPEG-WebP; /1.<ext> is served at full resolution).
        # Fall back to the traditional cover thumbnail when we can't build a
        # page-1 URL — that keeps behaviour on legacy / partial payloads.
        _cover_thumb = _cover_url_from_detail(data)
        _page1 = _page1_url_from_detail(data)
        return GalleryMeta(
            title=_pretty_title_from_detail(data),
            tags=_tag_names_from_detail(data),
            cover_url=_page1 or _cover_thumb,
            pages=int(data["num_pages"]) if data.get("num_pages") is not None else None,
            gallery_id=str(data.get("id") or gid),
            page1_url=_page1,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("nhentai: failed to normalise gallery %s: %s", gid, e)
        return None


# ---------------------------------------------------------------------------
# Public API — health_check() + route_status()
# ---------------------------------------------------------------------------
async def health_check() -> bool:
    """
    True iff we can currently hit nhentai's search endpoint and get a JSON
    body back. Used by /diag and startup_check.py.
    """
    data = await _http_get_json("/search", {"query": "test", "sort": "date", "page": 1})
    return bool(data and isinstance(data.get("result"), list))


def route_status() -> Dict[str, Any]:
    """Report the scraper's configuration, used by the /diag command."""
    return {
        "source": "nhentai.net",
        "endpoint": API_URL,
        "cache_entries": len(_cache),
        "inflight": len(_inflight),
        # Kept for compatibility with the earlier /diag layout that reported
        # proxy/scrapeapi configuration. Both False → no bypass service needed.
        "webshare": False,
        "scraperapi": False,
    }
