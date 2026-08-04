"""
scraper_bridge.py — Adapter around hf_scraper.py + direct nhentai v2 client.

NHENTAI v2 API SHAPES — verified live. Do not "simplify" these parsers.
=============================================================================
/api/v2/search rows have NO `title` dict and NO `tags` array:
    {"id": 227910, "media_id": "1200622",
     "english_title": "(C92) [Inariya] Kyoudai ... [English] [desudesu]",
     "japanese_title": "...", "thumbnail": "galleries/1200622/thumb.png",
     "num_pages": 28, "num_favorites": 169326, "tag_ids": [...]}
  * Reading row["title"] returns None → cards showed "#id".
  * Guessing "galleries/<mid>/cover.jpg" 404s on PNG galleries → broken
    images. ALWAYS use the row's own `thumbnail` path (extension varies).

/api/v2/galleries/<id> has full detail (different shape):
    {"title": {"english":..,"japanese":..,"pretty":..},
     "cover": {"path": "galleries/1584515/cover.jpg"},
     "scanlator": "", "upload_date": 1583615695,
     "num_pages": 65, "num_favorites": 150816,
     "tags": [{"type":"artist","name":"..","count":..}, ...]}

The OLD API (/api/galleries/search) returns HTTP 403 — never revert to it.
=============================================================================
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as _dt
import logging
import os
import re
import sys
from typing import Any

log = logging.getLogger("miniapp.scraper")

_HERE = os.path.dirname(os.path.abspath(__file__))
_BOT_ROOT = os.environ.get("MINIAPP_BOT_ROOT")
for _p in [
    _BOT_ROOT,
    os.path.abspath(os.path.join(_HERE, "..", "..", "..")),
    os.path.abspath(os.path.join(_HERE, "..", "..", "..", "..")),
    "/opt/render/project/src",
]:
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import hf_scraper as _hf
    HAVE_HF = True
    log.info("hf_scraper imported successfully")
except Exception as e:  # noqa: BLE001
    _hf = None
    HAVE_HF = False
    log.warning("hf_scraper not importable — using direct nhentai v2 client (%s)", e)


def _run_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


import httpx

_NH_V2 = "https://nhentai.net/api/v2"
_T_CDN = "https://t.nhentai.net"
_ENGLISH_TAG_ID = 12227
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://nhentai.net/",
}

_LEADING_EVENT = re.compile(r"^\s*\((?:[^()]|\([^()]*\))*\)\s*")
_LEADING_BRACKET = re.compile(r"^\s*\[(?:[^\[\]]|\[[^\[\]]*\])*\]\s*")
_TRAILING_BRACKET = re.compile(r"\s*\[([^\[\]]*)\]\s*$")
_KEEP_LEADING = {"anthology", "oneshot", "pixiv", "artbook", "webtoon"}
_DROP_TRAILING = {
    "english", "digital", "dl版", "中国翻訳", "chinese", "decensored",
    "colorized", "textless", "translated", "uncensored", "full color",
    "ongoing", "complete", "incomplete", "sample", "jp", "jpn", "kr",
}
_DROP_TRAILING_RE = re.compile(
    r"(scan|trans|censor|colori|digital|english|chinese|版|redraw|edit)",
    re.IGNORECASE,
)


def clean_title(raw: str) -> str:
    if not raw:
        return ""
    s = raw.strip()
    while True:
        m = _LEADING_EVENT.match(s)
        if not m: break
        s = s[m.end():]
    while True:
        m = _LEADING_BRACKET.match(s)
        if not m: break
        inner = m.group(0).strip()[1:-1].strip().lower()
        if inner in _KEEP_LEADING: break
        s = s[m.end():]
    while True:
        m = _TRAILING_BRACKET.search(s)
        if not m: break
        inner = m.group(1).strip()
        low = inner.lower()
        is_meta = (low in _DROP_TRAILING
                   or bool(_DROP_TRAILING_RE.search(low))
                   or len(inner) <= 24)
        if not is_meta: break
        candidate = s[: m.start()].rstrip()
        if not candidate: break
        s = candidate
    s = s.strip(" -–—|")
    return s or raw.strip()


def _thumb_from_search_row(row: dict) -> str:
    rel = (row.get("thumbnail") or "").lstrip("/")
    if rel:
        return f"{_T_CDN}/{rel}"
    mid = row.get("media_id")
    return f"{_T_CDN}/galleries/{mid}/thumb.jpg" if mid else ""


def _row_to_card(row: dict) -> dict:
    eng = (row.get("english_title") or "").strip()
    jpn = (row.get("japanese_title") or "").strip()
    pretty = clean_title(eng or jpn)
    gid = row.get("id")
    return {
        "id":             gid,
        "title":          pretty or (f"#{gid}" if gid is not None else ""),
        "title_english":  eng,
        "title_japanese": jpn,
        "cover":          _thumb_from_search_row(row),
        "pages":          row.get("num_pages"),
        "favorites":      row.get("num_favorites"),
        "tags":           [],
    }


def _direct_nhentai_search(q: str, page: int, sort: str) -> list[dict]:
    sort_map = {
        "popular": "popular", "popular-week": "popular-week",
        "popular-today": "popular-today", "date": "date", "recent": "date",
        "": "popular", None: "popular",
    }
    real_sort = sort_map.get((sort or "").lower(), "popular")
    query = q.strip() if q and q.strip() else "english"
    params = {"query": query, "sort": real_sort, "page": int(page or 1)}
    try:
        r = httpx.get(f"{_NH_V2}/search", params=params, headers=_HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        log.exception("v2 search failed q=%r sort=%r: %s", q, real_sort, e)
        return []
    out = []
    for row in data.get("result") or []:
        if _ENGLISH_TAG_ID not in (row.get("tag_ids") or []):
            continue
        if row.get("blacklisted"):
            continue
        out.append(_row_to_card(row))
    return out


_TAG_ORDER = ["parody", "character", "tag", "artist", "group", "language", "category"]


def _group_tags(tags):
    buckets = {}
    for t in tags or []:
        if not isinstance(t, dict):
            name = str(t).strip()
            if name:
                buckets.setdefault("tag", []).append({"name": name, "count": None})
            continue
        ttype = (t.get("type") or "tag").strip().lower()
        name = (t.get("name") or "").strip()
        if not name: continue
        buckets.setdefault(ttype, []).append({"name": name, "count": t.get("count")})
    ordered = {}
    for k in _TAG_ORDER:
        if k in buckets:
            ordered[k] = buckets.pop(k)
    ordered.update(buckets)
    return ordered


def _cover_from_detail(d):
    for key in ("cover", "thumbnail"):
        rel = ((d.get(key) or {}).get("path") or "").lstrip("/")
        if rel:
            return f"{_T_CDN}/{rel}"
    mid = d.get("media_id")
    return f"{_T_CDN}/galleries/{mid}/cover.jpg" if mid else ""


def _iso_date(ts):
    try:
        return _dt.datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _detail_to_dict(d):
    t = d.get("title")
    if isinstance(t, str):
        eng, jpn, pretty = t, "", clean_title(t)
    else:
        t = t or {}
        eng = (t.get("english") or "").strip()
        jpn = (t.get("japanese") or "").strip()
        pretty = (t.get("pretty") or "").strip() or clean_title(eng or jpn)
    groups = _group_tags(d.get("tags") or [])
    gid = d.get("id")
    return {
        "id":             gid,
        "title":          pretty or (f"#{gid}" if gid is not None else ""),
        "title_english":  eng,
        "title_japanese": jpn,
        "cover":          _cover_from_detail(d),
        "pages":          d.get("num_pages"),
        "favorites":      d.get("num_favorites"),
        "scanlator":      (d.get("scanlator") or "").strip(),
        "upload_date":    _iso_date(d.get("upload_date")),
        "tags":           [{"name": x["name"], "type": tt, "count": x.get("count")}
                           for tt, arr in groups.items() for x in arr],
        "groups":         groups,
    }


def _direct_nhentai_detail(gallery_id):
    try:
        r = httpx.get(f"{_NH_V2}/galleries/{gallery_id}",
                      params={"include": "related,suggestions,comments"},
                      headers=_HEADERS, timeout=15)
        r.raise_for_status()
        item = r.json() or {}
    except Exception as e:
        log.exception("v2 detail failed id=%r: %s", gallery_id, e)
        return {}
    return _detail_to_dict(item)


def _hit_to_dict(hit):
    if hit is None: return {}
    if dataclasses.is_dataclass(hit):
        d = dataclasses.asdict(hit)
    elif isinstance(hit, dict):
        d = hit
    else:
        d = {"gallery_id": getattr(hit, "gallery_id", None),
             "title":      getattr(hit, "title", None),
             "thumb_url":  getattr(hit, "thumb_url", None)}
    gid = d.get("gallery_id") or d.get("id")
    raw = d.get("title") or ""
    return {
        "id":            gid,
        "title":         clean_title(raw) or (f"#{gid}" if gid is not None else ""),
        "title_english": raw,
        "cover":         d.get("thumb_url") or d.get("cover") or "",
        "pages":         d.get("pages") or d.get("num_pages"),
        "tags":          d.get("tags") or [],
    }


def _meta_to_dict(meta):
    if meta is None: return {}
    if dataclasses.is_dataclass(meta):
        d = dataclasses.asdict(meta)
    elif isinstance(meta, dict):
        d = meta
    else:
        d = {"gallery_id": getattr(meta, "gallery_id", None),
             "title":      getattr(meta, "title", None),
             "cover_url":  getattr(meta, "cover_url", None),
             "pages":      getattr(meta, "pages", None),
             "tags":       getattr(meta, "tags", None)}
    groups = _group_tags(d.get("tags") or [])
    gid = d.get("gallery_id") or d.get("id")
    return {
        "id":            gid,
        "title":         d.get("title") or (f"#{gid}" if gid is not None else ""),
        "title_english": d.get("title") or "",
        "cover":         d.get("cover_url") or d.get("cover") or "",
        "pages":         d.get("pages") or d.get("num_pages"),
        "tags":          [{"name": x["name"], "type": tt, "count": x.get("count")}
                          for tt, arr in groups.items() for x in arr],
        "groups":        groups,
    }


def search(q, page, sort, lang, include_tags=None, exclude_tags=None,
           pages_min=None, pages_max=None, per_page=25):
    include_tags = include_tags or []
    exclude_tags = exclude_tags or []
    q_clean = (q or "").strip()
    rows = []
    if q_clean and HAVE_HF and hasattr(_hf, "search"):
        try:
            page_obj = _run_async(_hf.search(query=q_clean, page=int(page or 1)))
            if page_obj is not None:
                rows = [_hit_to_dict(h) for h in (getattr(page_obj, "hits", None) or [])]
        except Exception as e:
            log.exception("hf_scraper.search failed q=%r page=%s: %s", q_clean, page, e)
            rows = []
    if not rows:
        rows = _direct_nhentai_search(q_clean, int(page or 1), sort or "popular")
    rows = _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max)
    if per_page and per_page > 0:
        rows = rows[:per_page]
    return rows


def gallery_detail(gallery_id):
    detail = _direct_nhentai_detail(str(gallery_id))
    if detail and detail.get("id"):
        return detail
    if HAVE_HF and hasattr(_hf, "fetch_gallery_meta"):
        try:
            meta = _run_async(_hf.fetch_gallery_meta(str(gallery_id)))
            if meta is not None:
                d = _meta_to_dict(meta)
                if d.get("id"):
                    return d
        except Exception as e:
            log.exception("fetch_gallery_meta failed for %s: %s", gallery_id, e)
    return {}


def route_status():
    info = {"have_hf": HAVE_HF, "endpoint": _NH_V2}
    if HAVE_HF and hasattr(_hf, "route_status"):
        try: info["hf_route_status"] = _hf.route_status()
        except Exception as e: info["hf_route_status_error"] = str(e)
    if HAVE_HF and hasattr(_hf, "health_check"):
        try: info["hf_health_check"] = bool(_run_async(_hf.health_check()))
        except Exception as e: info["hf_health_check_error"] = str(e)
    if not HAVE_HF:
        info["source"] = "direct nhentai v2"
    return info


def _apply_filters(rows, include_tags, exclude_tags, pages_min, pages_max):
    def _pass(r):
        names = set()
        for t in (r.get("tags") or []):
            name = (t.get("name") if isinstance(t, dict) else str(t)) or ""
            if name:
                names.add(name.lower())
        if names:
            if include_tags and not all(t in names for t in include_tags):
                return False
            if exclude_tags and any(t in names for t in exclude_tags):
                return False
        p = int(r.get("pages") or r.get("num_pages") or 0)
        if pages_min is not None and p < pages_min:
            return False
        if pages_max is not None and p > pages_max:
            return False
        return True
    return [r for r in rows if _pass(r)]
