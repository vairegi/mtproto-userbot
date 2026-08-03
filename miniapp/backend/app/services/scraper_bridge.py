"""
scraper_bridge.py — Adapter around hf_scraper.py (parent project).

Isolates the Mini App from the exact API surface of hf_scraper. If the
scraper module later changes (e.g. we switch source sites again), only this
file needs updating.

Behaviour:
  * Tries to import hf_scraper from the parent project.  If unavailable
    (dev sandbox without the bot code), falls back to a direct nhentai
    JSON call so the frontend is still testable.
"""
from __future__ import annotations

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

    # Preferred path: use the bot's own scraper (respects its cache + filters).
    if HAVE_HF and hasattr(_hf, "search"):
        try:
            rows = _hf.search(query=q or "", page=page, sort=sort or "popular") or []
            return [_normalize(r, per_page)
                    for r in _apply_filters(rows, include_tags, exclude_tags,
                                            pages_min, pages_max)]
        except Exception as e:  # noqa: BLE001
            log.exception("hf_scraper.search failed, falling back: %s", e)

    rows = _fallback_search(q or "", page, sort or "popular")
    rows = _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max)
    return [_normalize(r, per_page) for r in rows]


def gallery_detail(gallery_id: str) -> dict:
    if HAVE_HF and hasattr(_hf, "fetch_gallery_meta"):
        try:
            return _normalize(_hf.fetch_gallery_meta(gallery_id), None) or {}
        except Exception as e:  # noqa: BLE001
            log.exception("hf_scraper.fetch_gallery_meta failed: %s", e)
    return _fallback_detail(str(gallery_id))


def route_status() -> dict:
    if HAVE_HF and hasattr(_hf, "route_status"):
        try:
            return _hf.route_status()
        except Exception as e:  # noqa: BLE001
            return {"error": str(e), "have_hf": True}
    return {"have_hf": HAVE_HF, "source": "fallback nhentai"}


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
