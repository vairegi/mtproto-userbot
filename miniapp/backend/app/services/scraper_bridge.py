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
# Async → sync helper
# ---------------------------------------------------------------------------
def _run_async(coro):
    """
    Run an awaitable to completion from a sync context.

    FastAPI's `def` (sync) endpoints run in a threadpool, so there is no
    running event loop on this thread. asyncio.run() creates and closes a
    fresh loop for each call — safe and simple.

    If a running loop is somehow present (async def endpoints, background
    tasks), we fall back to asyncio.new_event_loop() manually.
    """
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # An event loop is already running on this thread. Rare in this
        # codebase, but handle it defensively.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


# ---------------------------------------------------------------------------
# Direct nhentai fallback (used for empty/popular queries + on error)
# ---------------------------------------------------------------------------
import httpx

_NH_API = "https://nhentai.net/api"
_ENGLISH_TAG_ID = 12227   # matches hf_scraper's filter
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")


def _thumb_url_from_item(item: dict) -> str:
    """Build the cover URL from an nhentai search item."""
    media_id = item.get("media_id") or ""
    images = item.get("images") or {}
    cover = images.get("cover") or images.get("thumbnail") or {}
    ext_map = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
    ext = ext_map.get(cover.get("t", "j"), "jpg")
    return f"https://t.nhentai.net/galleries/{media_id}/cover.{ext}"


def _title_from_item(item: dict) -> str:
    t = item.get("title") or {}
    return t.get("english") or t.get("pretty") or t.get("japanese") or ""


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
    try:
        r = httpx.get(
            f"{_NH_API}/galleries/search",
            params=params,
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:  # noqa: BLE001
        log.exception("direct nhentai search failed q=%r sort=%r: %s", q, real_sort, e)
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


def _direct_nhentai_detail(gallery_id: str) -> dict:
    try:
        r = httpx.get(
            f"{_NH_API}/gallery/{gallery_id}",
            headers={"User-Agent": _UA, "Accept": "application/json"},
            timeout=15,
        )
        r.raise_for_status()
        item = r.json() or {}
    except Exception as e:  # noqa: BLE001
        log.exception("direct nhentai detail failed id=%r: %s", gallery_id, e)
        return {}

    return {
        "id":    item.get("id"),
        "title": _title_from_item(item),
        "cover": _thumb_url_from_item(item),
        "pages": item.get("num_pages"),
        "tags":  [{"name": t.get("name"), "type": t.get("type")}
                  for t in item.get("tags") or []],
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
    return {
        "id":    d.get("gallery_id") or d.get("id"),
        "title": d.get("title") or "",
        "cover": d.get("cover_url") or d.get("cover") or "",
        "pages": d.get("pages") or d.get("num_pages"),
        "tags":  tag_dicts,
    }


# ---------------------------------------------------------------------------
# Public API — called by routes/search.py and routes/gallery.py
# ---------------------------------------------------------------------------
def search(q: str, page: int, sort: str, lang: str,
           include_tags: list[str] | None = None,
           exclude_tags: list[str] | None = None,
           pages_min: int | None = None,
           pages_max: int | None = None,
           per_page: int = 25) -> list[dict]:
    """Return a list of normalized gallery dicts."""
    include_tags = include_tags or []
    exclude_tags = exclude_tags or []
    q_clean = (q or "").strip()

    rows: list[dict] = []

    # Path A: real query + hf_scraper available → prefer hf_scraper
    # (uses its cache + English-only filter + dedup).
    if q_clean and HAVE_HF and hasattr(_hf, "search"):
        try:
            page_obj = _run_async(_hf.search(query=q_clean, page=int(page or 1)))
            if page_obj is not None:
                hits = getattr(page_obj, "hits", None) or []
                rows = [_hit_to_dict(h) for h in hits]
        except Exception as e:  # noqa: BLE001
            log.exception("hf_scraper.search failed for q=%r page=%s: %s",
                          q_clean, page, e)
            rows = []

    # Path B: empty query OR hf_scraper returned nothing → direct nhentai.
    # This is what powers the Popular / Popular Week / Popular Today chips
    # on the default Discover view.
    if not rows:
        rows = _direct_nhentai_search(q_clean, int(page or 1), sort or "popular")

    # Filters + normalize
    rows = _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max)
    # Cap to per_page just in case upstream returned more.
    if per_page and per_page > 0:
        rows = rows[:per_page]
    return [_normalize(r) for r in rows]


def gallery_detail(gallery_id: str) -> dict:
    """Return the full detail dict for one gallery."""
    if HAVE_HF and hasattr(_hf, "fetch_gallery_meta"):
        try:
            meta = _run_async(_hf.fetch_gallery_meta(str(gallery_id)))
            if meta is not None:
                d = _meta_to_dict(meta)
                if d.get("id"):
                    return d
        except Exception as e:  # noqa: BLE001
            log.exception("hf_scraper.fetch_gallery_meta failed for %s: %s",
                          gallery_id, e)
    # Fallback
    return _direct_nhentai_detail(str(gallery_id))


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
