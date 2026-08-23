"""
meta.py — gallery metadata for the cover post.

v12.40d: CACHE-ONLY MODE. No upstream nhentai calls — Render's IP gets
403'd. If the Turso gallery row is missing or has no usable cover, we
return None and the caller marks FAILED_SCRAPE and moves on.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

log = logging.getLogger("bot2fetcher.meta")


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
    tags = []
    for t in (p.get("tags") or []):
        if isinstance(t, dict):
            name = t.get("name")
            if name:
                tags.append(str(name))
        elif t:
            tags.append(str(t))
    cover = p.get("cover") or p.get("cover_url") or p.get("thumb_url") or ""
    if cover and cover.startswith("//"):
        cover = "https:" + cover
    if not cover:
        log.warning("🔍 gallery:%s — no cover URL in payload; keys: %s", gid, list(p.keys())[:10])
        return None
    title = p.get("title_english") or p.get("title") or f"Gallery {gid}"
    pages = int(p.get("pages") or p.get("num_pages") or 0)
    log.info("🔍 gallery:%s — meta OK from Turso: %r, %d pages, cover %s",
             gid, title[:40], pages, cover[:60])
    return {
        "id": str(p.get("id")),
        "title": title,
        "cover": cover,
        "pages": pages,
        "tags": tags[:12],
    }


def caption_for(meta: Dict[str, Any]) -> str:
    gid = meta["id"]
    lines = [
        f"📖 {meta['title']}",
        f"🆔 {gid}",
        f"🔗 https://nhentai.net/g/{gid}/",
    ]
    if meta.get("pages"):
        lines.append(f"📄 {meta['pages']} pages")
    if meta.get("tags"):
        lines.append("🏷 " + ", ".join(meta["tags"]))
    return "\n".join(lines)[:1024]
