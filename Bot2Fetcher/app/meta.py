"""
meta.py — gallery metadata for the cover post.

Primary source: the Turso `gallery:<id>` cache row BOT 1 already wrote.
Fallback: nhentai's public JSON endpoint (only when cache row is missing).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

log = logging.getLogger("bot2fetcher.meta")


def meta_from_cache(gid: str, cache_row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not cache_row:
        return None
    p = cache_row.get("payload") or {}
    if not isinstance(p, dict) or not p.get("id"):
        return None
    tags = []
    for t in (p.get("tags") or []):
        if isinstance(t, dict):
            name = t.get("name")
            if name:
                tags.append(str(name))
        elif t:
            tags.append(str(t))
    cover = p.get("cover") or p.get("cover_url") or ""
    if cover and cover.startswith("//"):
        cover = "https:" + cover
    return {
        "id": str(p.get("id")),
        "title": p.get("title_english") or p.get("title") or f"Gallery {gid}",
        "cover": cover,
        "pages": int(p.get("pages") or 0),
        "tags": tags[:12],
    }


async def meta_from_upstream(gid: str) -> Optional[Dict[str, Any]]:
    try:
        import httpx
        url = f"https://nhentai.net/api/gallery/{gid}"
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                log.warning("upstream meta %s -> HTTP %s", gid, r.status_code)
                return None
            d = r.json()
        tags = [t.get("name") for t in (d.get("tags") or []) if t.get("name")]
        media = d.get("media_id")
        cover = f"https://t.nhentai.net/galleries/{media}/cover.jpg" if media else ""
        return {
            "id": str(gid),
            "title": (d.get("title") or {}).get("english") or f"Gallery {gid}",
            "cover": cover,
            "pages": int(d.get("num_pages") or 0),
            "tags": tags[:12],
        }
    except Exception as e:
        log.warning("upstream meta %s failed: %s", gid, e)
        return None


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
