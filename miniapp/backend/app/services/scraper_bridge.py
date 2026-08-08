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

import asyncio
import dataclasses
import logging
import os
import sys
import threading
import time as _time
from typing import Any, Optional

log = logging.getLogger("miniapp.scraper")

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
    _RATE_LIMIT_STRIKES[cache_key] = strikes + 1
    return int(max(_RATE_LIMIT_TTL_SEC, min(_RATE_LIMIT_TTL_CAP_SEC, dur)))
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


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
    query = q.strip() if q else "english"

    params = {"query": query, "sort": real_sort, "page": int(page or 1)}

    # v11.2: 429 back-off — short-circuit while the ban is live so we
    # don't hammer upstream and don't dump a full traceback per request.
    cache_key = ("search", query, real_sort, int(page or 1))
    now = _time.time()
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    if ban and ban > now:
        return []

    try:
        # v2 endpoint: /api/v2/search (params: query, sort, page)
        r = httpx.get(
            f"{_NH_API}/search",
            params=params,
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "Referer": "https://nhentai.net/",
            },
            timeout=15,
        )
        # v11.2: 429 is expected under load. Log at WARNING level ONCE
        # (not ERROR + full traceback every request) and cache the ban.
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After") if hasattr(r, "headers") else None
            dur = _rate_limit_backoff_sec(cache_key, retry_after)
            _RATE_LIMIT_CACHE[cache_key] = now + dur
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
            "cover": _thumb_url_from_item(item),
            "pages": item.get("num_pages"),
            "tags":  [{"name": t.get("name"), "type": t.get("type")}
                      for t in item.get("tags") or []],
        })
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


def _direct_nhentai_page1(item: dict) -> str:
    """Build the high-quality page-1 URL from a raw nhentai detail dict.

    Returns '' when the detail is missing `media_id` or `images.pages[0]`.
    Example: media_id=614941, pages[0].t='j' -> 
    'https://i.nhentai.net/galleries/614941/1.jpg'.
    """
    media_id = str(item.get("media_id") or "").strip()
    images = item.get("images") or {}
    pages = images.get("pages") if isinstance(images, dict) else None
    if not (media_id and isinstance(pages, list) and pages):
        return ""
    first = pages[0] if isinstance(pages[0], dict) else {}
    ext = _NH_EXT_MAP.get((first.get("t") or "j").strip().lower(), "jpg")
    return f"https://i.nhentai.net/galleries/{media_id}/1.{ext}"


def _direct_nhentai_detail(gallery_id: str) -> dict:
    # v11.2: 429 back-off cache (same rationale as _direct_nhentai_search).
    cache_key = ("detail", str(gallery_id))
    now = _time.time()
    ban = _RATE_LIMIT_CACHE.get(cache_key)
    if ban and ban > now:
        return {}
    try:
        # v2 endpoint for a single gallery: /api/v2/galleries/<id>
        r = httpx.get(
            f"{_NH_API}/galleries/{gallery_id}",
            headers={
                "User-Agent": _UA,
                "Accept": "application/json",
                "Referer": "https://nhentai.net/",
            },
            timeout=15,
        )
        # v11.2: 429 -> log ONCE at WARNING + back-off, no stack trace.
        if r.status_code == 429:
            retry_after = r.headers.get("Retry-After") if hasattr(r, "headers") else None
            dur = _rate_limit_backoff_sec(cache_key, retry_after)
            _RATE_LIMIT_CACHE[cache_key] = now + dur
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

    return {
        "id":       item.get("id"),
        "title":    clean_title(pretty) if pretty else _title_from_item(item),
        "title_english":  english_full,
        "title_japanese": japanese_full,
        "cover":    cover,
        # v11: expose page1_url separately alongside `cover`.
        "page1_url": page1,
        "pages":    item.get("num_pages"),
        "favorites": item.get("num_favorites"),
        "upload_date": _iso_date(item.get("upload_date")),
        "scanlator": item.get("scanlator") or "",
        "tags":     flat_tags,
        "tag_groups": groups,
    }


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
    return {
        "id":    d.get("gallery_id") or d.get("id"),
        "title": d.get("title") or "",
        "cover": d.get("thumb_url") or d.get("cover") or d.get("cover_url") or "",
        "pages": d.get("pages") or d.get("num_pages"),
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
    query = q_clean.strip() if q_clean else "english"
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
    q_clean = (q or "").strip()
    per_page = int(per_page) if per_page and per_page > 0 else 25

    try:
        max_upstream = int(os.environ.get(
            "MINIAPP_SEARCH_MAX_UPSTREAM_PAGES", _MAX_UPSTREAM_PAGES_DEFAULT))
    except (TypeError, ValueError):
        max_upstream = _MAX_UPSTREAM_PAGES_DEFAULT

    start_offset = (max(1, int(page or 1)) - 1) * per_page
    want_total   = start_offset + per_page

    collected: list[dict] = []
    upstream_page = 1
    consecutive_empty = 0
    rate_limited_pages: list[int] = []

    while len(collected) < want_total and upstream_page <= max_upstream:
        rows: list[dict] = []
        if q_clean and HAVE_HF and hasattr(_hf, "search"):
            try:
                page_obj = _run_async(_hf.search(query=q_clean, page=upstream_page))
                if page_obj is not None:
                    hits = getattr(page_obj, "hits", None) or []
                    rows = [_hit_to_dict(h) for h in hits]
            except Exception as e:  # noqa: BLE001
                log.exception("hf_scraper.search failed for q=%r page=%s: %s",
                              q_clean, upstream_page, e)
                rows = []

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

    window = collected[start_offset:start_offset + per_page]
    items = [_normalize(r) for r in window]

    if not _return_meta:
        return items
    return {
        "items": items,
        # has_more: we EITHER filled the window AND some upstream cushion
        # remains, OR we didn't fill it but had to give up early due to
        # 429s (client can retry). Both signals produce a truthful button.
        "has_more": (
            len(collected) > start_offset + per_page
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


def gallery_detail(gallery_id: str) -> dict:
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
