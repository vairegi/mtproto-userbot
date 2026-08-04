"""
scraper_bridge.py — Adapter around hf_scraper.py (parent project).

Isolates the Mini App from the exact API surface of hf_scraper. If the
scraper module later changes (e.g. we switch source sites again), only this
file needs updating.

Behaviour:
  * Tries to import hf_scraper from the parent project. If unavailable
    (dev sandbox without the bot code), falls back to a direct nhentai
    JSON call so the frontend is still testable.
  * Uses `inspect.signature` to pass only the kwargs hf_scraper.search
    actually accepts — this makes us resilient to renames like
    sort → sort_by, query → q, page → pg, etc. without ever crashing
    with TypeError.
"""
from __future__ import annotations

import inspect
import logging
import os
import sys
from typing import Any

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
    import hf_scraper as _hf   # noqa: E402  parent project's module
    HAVE_HF = True
    log.info("hf_scraper imported successfully")
except Exception as e:  # noqa: BLE001
    _hf = None
    HAVE_HF = False
    log.warning("hf_scraper not importable — using fallback nhentai client (%s)", e)


# --- Signature-safe kwarg passing ---------------------------------------
# Build a set of parameter names hf_scraper.search actually accepts, so we
# never pass an unexpected kwarg. Precomputed once at import for speed.
_HF_SEARCH_PARAMS: set[str] = set()
if HAVE_HF and hasattr(_hf, "search"):
    try:
        sig = inspect.signature(_hf.search)
        for name, p in sig.parameters.items():
            # Exclude *args / **kwargs sentinels from the whitelist.
            if p.kind in (inspect.Parameter.VAR_POSITIONAL,
                          inspect.Parameter.VAR_KEYWORD):
                continue
            _HF_SEARCH_PARAMS.add(name)
        log.info("hf_scraper.search accepts kwargs: %s", sorted(_HF_SEARCH_PARAMS))
    except (TypeError, ValueError) as e:
        log.warning("Could not introspect hf_scraper.search signature: %s", e)


def _call_hf_search(**kwargs) -> list[dict]:
    """
    Call hf_scraper.search with only the kwargs it actually accepts.

    Maps common name variants so the caller can use one canonical set of
    parameter names (q, page, sort, per_page, lang) regardless of what
    hf_scraper's real signature happens to be.
    """
    # Canonical name → list of aliases hf_scraper.search might accept.
    aliases = {
        "q":        ["q", "query", "search", "keyword", "text"],
        "page":     ["page", "pg", "p", "page_num"],
        "sort":     ["sort", "sort_by", "sort_mode", "order", "order_by"],
        "per_page": ["per_page", "page_size", "limit", "count"],
        "lang":     ["lang", "language"],
    }
    to_pass: dict[str, Any] = {}
    for canonical, val in kwargs.items():
        if val is None:
            continue
        candidates = aliases.get(canonical, [canonical])
        for alias in candidates:
            if alias in _HF_SEARCH_PARAMS:
                to_pass[alias] = val
                break
        # If NO alias matched, we silently drop the arg rather than crash.
        # The scraper simply won't filter by that dimension.

    return _hf.search(**to_pass) or []


# --- Fallback client (only used if hf_scraper import fails) ---------------
if not HAVE_HF:
    import httpx

    _NH_BASE = "https://nhentai.net"

    def _fallback_search(q: str, page: int, sort: str, **_) -> list[dict]:
        params = {"query": q or "english", "page": page}
        if sort and sort != "popular":
            params["sort"] = sort
        r = httpx.get(f"{_NH_BASE}/api/galleries/search", params=params, timeout=15)
        r.raise_for_status()
        data = r.json() or {}
        out = []
        for item in data.get("result", []):
            titles = item.get("title", {}) or {}
            out.append({
                "id": item.get("id") or item.get("media_id"),
                "title": titles.get("english") or titles.get("pretty") or titles.get("japanese") or "",
                "cover": _thumb_url(item),
                "pages": item.get("num_pages"),
                "tags":  [{"name": t.get("name"), "type": t.get("type")}
                          for t in item.get("tags") or []],
            })
        return out

    def _fallback_detail(gid: str) -> dict:
        r = httpx.get(f"{_NH_BASE}/api/gallery/{gid}", timeout=15)
        r.raise_for_status()
        item = r.json() or {}
        titles = item.get("title", {}) or {}
        return {
            "id": item.get("id"),
            "title": titles.get("english") or titles.get("pretty") or titles.get("japanese") or "",
            "cover": _thumb_url(item),
            "pages": item.get("num_pages"),
            "tags":  [{"name": t.get("name"), "type": t.get("type")}
                      for t in item.get("tags") or []],
        }

    def _thumb_url(item: dict) -> str:
        media_id = item.get("media_id")
        images = item.get("images", {}) or {}
        cover = images.get("cover") or images.get("thumbnail") or {}
        ext_map = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
        ext = ext_map.get(cover.get("t", "j"), "jpg")
        return f"https://t.nhentai.net/galleries/{media_id}/cover.{ext}"


# --- Public API (used by routes) -----------------------------------------
def search(q: str, page: int, sort: str, lang: str,
           include_tags: list[str] | None = None,
           exclude_tags: list[str] | None = None,
           pages_min: int | None = None,
           pages_max: int | None = None,
           per_page: int = 25) -> list[dict]:
    """Return a list of normalized gallery dicts."""
    include_tags = include_tags or []
    exclude_tags = exclude_tags or []

    # Preferred path: the bot's own hf_scraper (respects its cache + filters).
    if HAVE_HF and hasattr(_hf, "search"):
        try:
            rows = _call_hf_search(
                q=q or "",
                page=page,
                sort=sort or "popular",
                per_page=per_page,
                lang=lang or "english",
            )
            return [_normalize(r, per_page)
                    for r in _apply_filters(rows, include_tags, exclude_tags,
                                            pages_min, pages_max)]
        except Exception as e:  # noqa: BLE001
            log.exception("hf_scraper.search failed, falling back to direct nhentai: %s", e)

    # Fallback: direct nhentai call.
    if HAVE_HF:
        # hf_scraper imported OK but crashed — we still need a fallback.
        import httpx
        _NH_BASE = "https://nhentai.net"
        params = {"query": q or "english", "page": page}
        if sort and sort != "popular":
            params["sort"] = sort
        try:
            r = httpx.get(f"{_NH_BASE}/api/galleries/search", params=params, timeout=15)
            r.raise_for_status()
            data = r.json() or {}
            rows = []
            for item in data.get("result", []):
                titles = item.get("title", {}) or {}
                media_id = item.get("media_id")
                images = item.get("images", {}) or {}
                cover = images.get("cover") or images.get("thumbnail") or {}
                ext_map = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
                ext = ext_map.get(cover.get("t", "j"), "jpg")
                rows.append({
                    "id": item.get("id") or item.get("media_id"),
                    "title": titles.get("english") or titles.get("pretty") or titles.get("japanese") or "",
                    "cover": f"https://t.nhentai.net/galleries/{media_id}/cover.{ext}",
                    "pages": item.get("num_pages"),
                    "tags":  [{"name": t.get("name"), "type": t.get("type")}
                              for t in item.get("tags") or []],
                })
        except Exception as e:  # noqa: BLE001
            log.exception("Direct nhentai fallback also failed: %s", e)
            return []
    else:
        rows = _fallback_search(q or "", page, sort or "popular")

    rows = _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max)
    return [_normalize(r, per_page) for r in rows]


def gallery_detail(gallery_id: str) -> dict:
    if HAVE_HF and hasattr(_hf, "fetch_gallery_meta"):
        try:
            return _normalize(_hf.fetch_gallery_meta(gallery_id), None) or {}
        except Exception as e:  # noqa: BLE001
            log.exception("hf_scraper.fetch_gallery_meta failed: %s", e)
    # Direct fallback for detail
    try:
        import httpx
        r = httpx.get(f"https://nhentai.net/api/gallery/{gallery_id}", timeout=15)
        r.raise_for_status()
        item = r.json() or {}
        titles = item.get("title", {}) or {}
        media_id = item.get("media_id")
        images = item.get("images", {}) or {}
        cover = images.get("cover") or images.get("thumbnail") or {}
        ext_map = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}
        ext = ext_map.get(cover.get("t", "j"), "jpg")
        return {
            "id": item.get("id"),
            "title": titles.get("english") or titles.get("pretty") or titles.get("japanese") or "",
            "cover": f"https://t.nhentai.net/galleries/{media_id}/cover.{ext}",
            "pages": item.get("num_pages"),
            "tags":  [{"name": t.get("name"), "type": t.get("type")}
                      for t in item.get("tags") or []],
        }
    except Exception as e:  # noqa: BLE001
        log.exception("gallery_detail fallback failed: %s", e)
        return {}


def route_status() -> dict:
    """Diagnostics for /api/admin/diag."""
    info: dict[str, Any] = {
        "have_hf": HAVE_HF,
        "hf_search_params": sorted(_HF_SEARCH_PARAMS) if HAVE_HF else [],
    }
    if HAVE_HF and hasattr(_hf, "route_status"):
        try:
            info["hf_route_status"] = _hf.route_status()
        except Exception as e:  # noqa: BLE001
            info["hf_route_status_error"] = str(e)
    if not HAVE_HF:
        info["source"] = "fallback nhentai"
    return info


# --- Helpers -------------------------------------------------------------
def _normalize(row: dict, per_page: int | None) -> dict:
    if not row:
        return {}
    return {
        "id":    row.get("id") or row.get("gallery_id"),
        "title": row.get("title") or row.get("english_title") or "",
        "cover": row.get("cover") or row.get("cover_url") or "",
        "pages": row.get("pages") or row.get("num_pages"),
        "tags":  row.get("tags") or [],
    }


def _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max):
    def _pass(r):
        tag_names = {(t.get("name") if isinstance(t, dict) else str(t)).lower()
                     for t in (r.get("tags") or [])}
        if include_tags and not all(t in tag_names for t in include_tags):
            return False
        if exclude_tags and any(t in tag_names for t in exclude_tags):
            return False
        p = r.get("pages") or r.get("num_pages") or 0
        if pages_min is not None and p < pages_min: return False
        if pages_max is not None and p > pages_max: return False
        return True
    return [r for r in rows if _pass(r)]
