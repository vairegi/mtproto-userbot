"""
meta.py — gallery metadata + Bot-0-exact caption builder.

v12.40e: caption is now byte-compatible with repo-root cover_poster.py
`_format_caption` (v12.34l): bold clean title, blank line, '➤ #<gid>',
blank line, aligned meta rows (Groups → Parodies → Artists → Characters
→ Languages → Categories), blank line, '➤ Tags:' row. NO nhentai URL,
NO pages line, NO 🆔 emoji. Tags keep their {'name','type'} shape from
the Turso cache payload so grouping works exactly like Bot 0.
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
    """Bot-0-exact cover caption (see cover_poster._format_caption)."""
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
    """Build meta from the Turso gallery:<id> payload. CACHE-ONLY — never
    call nhentai. Returns None (caller marks FAILED_SCRAPE) when unusable."""
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

    # Keep tags TYPED (name+type) so caption grouping matches Bot 0.
    tags_typed: List[Dict[str, str]] = []
    for t in (p.get("tags") or []):
        if isinstance(t, dict):
            nm = t.get("name")
            if nm:
                tags_typed.append({"name": str(nm), "type": str(t.get("type") or "tag")})
        elif t:
            tags_typed.append({"name": str(t), "type": "tag"})

    cover = p.get("cover") or p.get("cover_url") or p.get("thumb_url") or ""
    if cover and cover.startswith("//"):
        cover = "https:" + cover
    if not cover:
        log.warning("🔍 gallery:%s — no cover URL in payload; keys: %s", gid, list(p.keys())[:10])
        return None

    title = p.get("title_english") or p.get("title") or f"Gallery {gid}"
    pages = int(p.get("pages") or p.get("num_pages") or 0)
    log.info("🔍 gallery:%s — meta OK from Turso: %r, %d pages, %d typed tags",
             gid, title[:40], pages, len(tags_typed))
    return {
        "id": str(p.get("id")),
        "title": title,
        "cover": cover,
        "pages": pages,
        "tags": tags_typed,
    }
