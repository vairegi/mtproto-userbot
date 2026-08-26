"""
meta.py — gallery metadata + Bot-0-exact caption builder.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger("bot2fetcher.meta")

_TAG_SANITISE_RE = re.compile(r"[^A-Za-z0-9_]+")
_EVENT_PREFIX_RE = re.compile(r"^\([A-Za-z0-9+\- ]+\)\s*")
_BRACKET_TAIL_RE = re.compile(r"(\[[^\]]*\])\s*$")
_CAPTION_HARD_LIMIT = 1024
_TAGS_ROW_MAX = 600

# v12.43: nhentai CDN constants for cover-URL construction when the cached
# payload has no usable cover URL (legacy rows store CDN-relative paths or
# v1-style dicts instead of a URL string).
_T_CDN = "https://t.nhentai.net"
_NH_EXT_MAP = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}


def _construct_cover_url(gid: str, p: Dict[str, Any]) -> str:
    """Build the cover URL ourselves when extraction from `cover` failed.

    Handles every payload shape seen in the wild:
      * thumbnail / cover as string   — CDN-relative path or full URL
      * thumbnail / cover as v2 dict  — {"path": "galleries/<mid>/cover.jpg"}
      * thumbnail / cover as v1 dict  — {"t": "j"} + media_id on the payload
      * nothing usable                — canonical guess from media_id
        (https://t.nhentai.net/galleries/<media_id>/cover.jpg); the fetcher
        retries other extensions if this 404s.
    Returns "" only when there is no media_id to build from."""
    media_id = str(p.get("media_id") or "").strip()

    # Pass 1: explicit values win — string or dict-with-path/url, either
    # field. An EMPTY dict ({} as in legacy rows) must NOT shadow a real
    # value in the other field, so dicts with no path/url fall through.
    for field in ("thumbnail", "cover"):
        v = p.get(field)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if s.startswith("//"):
                return "https:" + s
            if s.startswith("http"):
                return s
            return _T_CDN + "/" + s.lstrip("/")
        if isinstance(v, dict):
            path = str(v.get("path") or "").strip()
            if path:
                if path.startswith("//"):
                    return "https:" + path
                if path.startswith("http"):
                    return path
                return _T_CDN + "/" + path.lstrip("/")
            url = str(v.get("url") or v.get("src") or "").strip()
            if url:
                if url.startswith("//"):
                    return "https:" + url
                if url.startswith("http"):
                    return url
                return _T_CDN + "/" + url.lstrip("/")

    # Pass 2: v1-style {"t": "j"} dicts — need media_id to build the path.
    if media_id:
        for field in ("cover", "thumbnail"):
            v = p.get(field)
            if isinstance(v, dict) and v:
                ext = _NH_EXT_MAP.get(
                    str(v.get("t") or "j").strip().lower(), "jpg")
                name = "cover" if field == "cover" else "thumb"
                return f"{_T_CDN}/galleries/{media_id}/{name}.{ext}"

    # Pass 3: bare media_id guess — fetcher retries other extensions.
    if media_id:
        log.info("🔍 gallery:%s — cover not in payload; constructed CDN "
                 "URL from media_id=%s", gid, media_id)
        return f"{_T_CDN}/galleries/{media_id}/cover.jpg"
    return ""

_META_ROW_ORDER = [
    ("group",     "Groups"),
    ("parody",    "Parodies"),
    ("artist",    "Artists"),
    ("character", "Characters"),
    ("language",  "Languages"),
    ("category",  "Categories"),
]


def _hashtagify(tag: str) -> str:
    if not tag:
        return ""
    cleaned = _TAG_SANITISE_RE.sub("_", str(tag).strip()).strip("_")
    return f"#{cleaned}" if cleaned else ""


def _clean_title(raw: str) -> str:
    s = (raw or "").strip()
    s = _EVENT_PREFIX_RE.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _BRACKET_TAIL_RE.sub("", s).strip()
    return s or (raw or "").strip()


def _group_tags_by_type(tags) -> dict:
    groups: dict = {}
    for t in tags or []:
        if isinstance(t, dict):
            typ = str(t.get("type") or "tag").lower()
            nm = str(t.get("name") or "").strip()
        else:
            typ = "tag"
            nm = str(t or "").strip()
        if nm:
            groups.setdefault(typ, []).append(nm)
    return groups


def caption_for(meta: Dict[str, Any]) -> str:
    lines: List[str] = []
    clean = _clean_title(meta.get("title") or "") or "(untitled)"
    lines.append(f"**{clean}**")

    gid = str(meta.get("id") or "").strip().lstrip("#")
    if gid:
        lines.append("")
        lines.append(f"➤ #{gid}")

    groups = _group_tags_by_type(meta.get("tags") or [])
    label_width = max(len(lbl) for _, lbl in _META_ROW_ORDER) + 1
    meta_lines: List[str] = []
    for key, label in _META_ROW_ORDER:
        names = groups.get(key) or []
        hashtags = [h for h in (_hashtagify(n) for n in names) if h]
        if hashtags:
            col = (label + ":").ljust(label_width)
            meta_lines.append(f"➤ {col} {' '.join(hashtags)}")
    if meta_lines:
        lines.append("")
        lines.extend(meta_lines)

    plain_tags = groups.get("tag") or []
    if plain_tags:
        hashtags = [h for h in (_hashtagify(n) for n in plain_tags) if h]
        if hashtags:
            joined = " ".join(hashtags)
            if len(joined) > _TAGS_ROW_MAX:
                joined = joined[:_TAGS_ROW_MAX].rsplit(" ", 1)[0]
            col = "Tags:".ljust(label_width)
            lines.append("")
            lines.append(f"➤ {col} {joined}")

    out = "\n".join(lines)
    if len(out) > _CAPTION_HARD_LIMIT:
        out = out[:_CAPTION_HARD_LIMIT - 3] + "..."
    return out


def meta_from_cache(gid: str, cache_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cache_row:
        log.info("🔍 gallery:%s — no Turso cache row", gid)
        return None
    p = cache_row.get("payload") or {}
    if not isinstance(p, dict):
        log.warning("🔍 gallery:%s — payload is not a dict (%s)", gid, type(p).__name__)
        return None
    if not p.get("id"):
        log.warning("🔍 gallery:%s — payload has no .id; keys: %s", gid, list(p.keys())[:10])
        return None

    tags_typed: List[Dict[str, str]] = []
    for t in (p.get("tags") or []):
        if isinstance(t, dict):
            nm = t.get("name")
            if nm:
                tags_typed.append({"name": str(nm), "type": str(t.get("type") or "tag")})
        elif t:
            tags_typed.append({"name": str(t), "type": "tag"})

    cover = p.get("cover") or p.get("cover_url") or p.get("thumb_url") or ""

    # Fix: Safely handle cover if dictionary object is returned
    if isinstance(cover, dict):
        cover = cover.get("url") or cover.get("src") or ""

    # Fix: Ensure type string before performing startswith
    if isinstance(cover, str):
        if cover.startswith("//"):
            cover = "https:" + cover
    else:
        cover = ""

    if not cover:
        # v12.43: do NOT drop — construct the CDN URL from media_id (the
        # "download it yourself" path). Only a payload with no media_id at
        # all is still undeliverable.
        cover = _construct_cover_url(str(p.get("id") or gid), p)
    if not cover:
        log.warning("🔍 gallery:%s — no cover URL and no media_id in "
                    "payload; keys: %s", gid, list(p.keys())[:10])
        return None

    title = p.get("title_english") or p.get("title") or f"Gallery {gid}"
    
    # Naya logic jo pages ko safe banayega aur list aane par list ke andar ka number nikalega
    raw_pages = p.get("pages") or p.get("num_pages") or 0
    if isinstance(raw_pages, list):
        raw_pages = raw_pages[0] if raw_pages else 0
        
    pages = int(raw_pages)
    
    log.info("🔍 gallery:%s — meta OK from Turso: %r, %d pages, %d typed tags",
             gid, title[:40], pages, len(tags_typed))
    return {
        "id": str(p.get("id")),
        "title": title,
        "cover": cover,
        "pages": pages,
        "tags": tags_typed,
    }
