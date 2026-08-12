"""
source_api.py — Optional fallback used when Bot 1's post is not detected
in time (Brief §7).

If SOURCE_API_BASE / SOURCE_API_KEY are empty in .env, `fetch_metadata`
returns None and the relay flow falls through to the "PDF alone / partial"
branch.

The exact endpoint shape is unknown at build time — Ryan will supply it.
The function below is a shape-agnostic best-effort call: it does a GET on
`{SOURCE_API_BASE}?url=<gallery_url>` with an `X-API-Key` header, and looks
for a JSON payload with `title`, `tags`, and `cover_url` fields. Adjust
this one function once Ryan has the real endpoint doc.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import httpx

from config import settings
from logging_setup import setup_logging

log = setup_logging("source_api")


@dataclass
class GalleryMeta:
    title: str
    tags: List[str]
    cover_url: Optional[str]


async def fetch_metadata(gallery_url: str) -> Optional[GalleryMeta]:
    if not settings.source_api_base or not settings.source_api_key:
        return None

    headers = {"X-API-Key": settings.source_api_key, "Accept": "application/json"}
    params = {"url": gallery_url}

    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(settings.source_api_base, headers=headers, params=params)
            if r.status_code != 200:
                log.warning("source_api HTTP %s for %s", r.status_code, gallery_url)
                return None
            data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("source_api call failed: %s", e)
        return None

    title = str(data.get("title") or "").strip()
    tags_raw = data.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    else:
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    cover_url = data.get("cover_url") or data.get("cover") or None

    if not title:
        return None
    return GalleryMeta(title=title, tags=tags, cover_url=cover_url)


def format_fallback_caption(meta: GalleryMeta) -> str:
    tags_line = " ".join(f"#{t.replace(' ', '_')}" for t in meta.tags) if meta.tags else ""
    parts = [meta.title]
    if tags_line:
        parts.append("")
        parts.append(tags_line)
    return "\n".join(parts)
