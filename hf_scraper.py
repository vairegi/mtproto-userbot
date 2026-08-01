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

# nhentai's own frontend uses this exact User-Agent flavour + Accept header;
# copying it keeps our profile identical to what their WAF already whitelists.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    # No 'br' — we don't ship the brotli package, so gzip/deflate is the
    # safe, universal choice. nhentai serves fine over gzip.
    "Accept-Encoding": "gzip, deflate",
    "Referer": f"{BASE_URL}/",
}

_TIMEOUT = 20.0
_SEARCH_CACHE_TTL_SEC = 90
_GALLERY_CACHE_TTL_SEC = 30 * 60
_CACHE_MAX_ENTRIES = 128


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
    title: str
    tags: List[str]
    cover_url: Optional[str]
    pages: Optional[int] = None
    gallery_id: Optional[str] = None


# ---------------------------------------------------------------------------
# HTTP layer — one shared AsyncClient, plain httpx GETs to nhentai
# ---------------------------------------------------------------------------
_client_lock = threading.Lock()
_client: Optional[httpx.AsyncClient] = None


async def _get_client() -> httpx.AsyncClient:
    """Return the shared AsyncClient, creating on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.AsyncClient(
                    timeout=_TIMEOUT,
                    follow_redirects=True,
                    headers=_HEADERS,
                    http2=False,   # nhentai serves fine over http/1.1
                )
    return _client


async def _http_get_json(path: str, params: Optional[dict] = None) -> Optional[dict]:
    """
    GET a nhentai JSON endpoint and return its parsed body, or None on failure.
    Never raises upward — callers get None and can surface a clean UX error.
    """
    url = f"{API_URL}{path}"
    try:
        client = await _get_client()
        r = await client.get(url, params=params)
        if r.status_code != 200:
            log.warning("nhentai HTTP %s for %s params=%s", r.status_code, path, params)
            return None
        try:
            return r.json()
        except json.JSONDecodeError as e:
            log.warning("nhentai returned non-JSON (%s) for %s: %s",
                        e, path, r.text[:120])
            return None
    except Exception as e:  # noqa: BLE001
        log.warning("nhentai request failed for %s: %s", path, e)
        return None


async def _fetch_json_cached(cache_key: str, path: str,
                             params: Optional[dict], ttl_sec: int) -> Optional[dict]:
    """
    JSON fetch with:
      - response cache (ttl_sec)
      - in-flight dedup (two concurrent identical requests share one call)
    """
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

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

    try:
        data = await _http_get_json(path, params)
        if data is not None:
            _cache_put(cache_key, data, ttl_sec)
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
    en = (item.get("english_title") or "").strip()
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


def _tag_names_from_detail(detail: dict) -> List[str]:
    """Extract useful tag names. Keeps the fields /mpost traditionally used."""
    out: List[str] = []
    for t in detail.get("tags") or []:
        if not isinstance(t, dict):
            continue
        ttype = (t.get("type") or "").strip().lower()
        name = (t.get("name") or "").strip()
        if not name:
            continue
        if ttype in ("tag", "artist", "parody", "character", "group"):
            out.append(name)
    return out


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

    data = await _fetch_json_cached(cache_key, "/search", params, _SEARCH_CACHE_TTL_SEC)
    if data is None:
        return None

    try:
        results = data.get("result") or []
        num_pages = int(data.get("num_pages") or 1)
        per_page = int(data.get("per_page") or len(results) or 25)
        total = int(data.get("total") or (num_pages * per_page))

        hits: List[SearchHit] = []
        for item in results:
            gid = item.get("id")
            if gid is None:
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
        return GalleryMeta(
            title=_pretty_title_from_detail(data),
            tags=_tag_names_from_detail(data),
            cover_url=_cover_url_from_detail(data),
            pages=int(data["num_pages"]) if data.get("num_pages") is not None else None,
            gallery_id=str(data.get("id") or gid),
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
