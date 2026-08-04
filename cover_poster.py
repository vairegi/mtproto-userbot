"""
cover_poster.py — In-house replacement for Bot 1.

For each accepted gallery job, this module:
  1. Scrapes gallery metadata (title, tags, cover_url, pages) via hf_scraper.
  2. Downloads the cover image bytes.
  3. Posts them to the Database Channel via the userbot session, with a
     caption in the same shape Bot 1 used to produce, so downstream tooling
     (progress tracker, mini-app "Open Post" deep-links, /mpost caption
     matcher) does not need to change.

It also exposes:
  - `delete_cover(client, msg_id)` — used on FAILED_BOT2_ERROR to remove the
    cover post so the channel stays clean.
  - `build_open_link(channel_id, msg_id)` — produce a t.me/c/<id>/<msg>
    deep-link that goes into `galleries.open_link`.

Design notes
------------
- The module NEVER touches the Mongo `galleries` doc directly; that is the
  caller's job (via `gallery_state`). This keeps cover_poster testable
  against a fake TelegramClient with no DB dependency.
- Cover download uses httpx (already a project dep) with a browser-like
  User-Agent + Referer so nhentai's t.nhentai.net CDN doesn't 403 us.
- If the cover download fails, we still post a text-only caption so the
  PDF has something to reply to. That is important: without a cover post
  the "forward PDF as reply" step has nothing to attach to.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

import httpx
from telethon import TelegramClient
from telethon.errors import FloodWaitError, MessageDeleteForbiddenError

import hf_scraper
from config import settings

log = logging.getLogger("cover_poster")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": "https://nhentai.net/",
}
_COVER_DL_TIMEOUT = 20.0        # seconds
_CAPTION_HARD_LIMIT = 1024      # Telegram caption limit (safety margin: 1000)


# ---------------------------------------------------------------------------
# Public data shape
# ---------------------------------------------------------------------------

@dataclass
class CoverPost:
    """Result of a successful cover-post op."""
    msg_id: int
    channel_id: int
    open_link: str
    title: str
    pages: Optional[int]
    tags: List[str]
    cover_url: Optional[str]
    used_fallback_text_only: bool = False   # True if cover download failed


# ---------------------------------------------------------------------------
# Deep-link builder
# ---------------------------------------------------------------------------

def build_open_link(channel_id: int, msg_id: int) -> str:
    """Return a `https://t.me/c/<internal>/<msg_id>` link for a private channel.

    Telegram's private-channel deep-link uses the channel's raw ID with the
    -100 prefix stripped. We accept either form and produce the canonical
    link. Callers should pass `settings.database_channel_id`.
    """
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return ""
    # Channel IDs in the -100XXXXXXXXXX form: strip the leading -100 for t.me/c
    s = str(abs(cid))
    if s.startswith("100"):
        s = s[3:]
    return f"https://t.me/c/{s}/{int(msg_id)}"


# ---------------------------------------------------------------------------
# Caption formatting (mirrors Bot 1's shape as closely as possible)
# ---------------------------------------------------------------------------

_TAG_SANITISE_RE = re.compile(r"[^A-Za-z0-9_]+")


def _hashtagify(tag: str) -> str:
    """Turn 'big breasts' → '#big_breasts', collapsing weird chars."""
    if not tag:
        return ""
    cleaned = _TAG_SANITISE_RE.sub("_", str(tag).strip()).strip("_")
    return f"#{cleaned}" if cleaned else ""


def _format_caption(
    title: str,
    tags: List[str],
    pages: Optional[int],
    url: str,
    requester_handle: Optional[str] = None,
) -> str:
    """Build the cover-post caption. Kept close to Bot 1's original format so
    the progress tracker's regexes and the /mpost caption matcher continue
    to work without changes.
    """
    lines: List[str] = []

    header = title.strip() if title else "(untitled)"
    if pages:
        header = f"{header}  ·  {int(pages)}p"
    lines.append(header)

    if requester_handle:
        h = requester_handle if requester_handle.startswith("@") else f"@{requester_handle}"
        lines.append(f"requested by {h}")

    if url:
        lines.append("")
        lines.append(url)

    hashtags = [t for t in (_hashtagify(x) for x in (tags or [])) if t]
    if hashtags:
        # Cap the hashtag block so we don't blow the caption limit.
        joined = " ".join(hashtags)
        if len(joined) > 600:
            joined = joined[:600].rsplit(" ", 1)[0]
        lines.append("")
        lines.append(joined)

    out = "\n".join(lines)
    if len(out) > _CAPTION_HARD_LIMIT:
        out = out[: _CAPTION_HARD_LIMIT - 3] + "..."
    return out


# ---------------------------------------------------------------------------
# Cover download (isolated so it can be swapped for a fake in tests)
# ---------------------------------------------------------------------------

async def _download_cover(cover_url: str) -> Optional[bytes]:
    """Fetch the cover image bytes, or None on any failure."""
    if not cover_url:
        return None
    try:
        async with httpx.AsyncClient(
            timeout=_COVER_DL_TIMEOUT,
            headers=_HEADERS,
            follow_redirects=True,
        ) as c:
            r = await c.get(cover_url)
            if r.status_code != 200:
                log.warning("cover download %s → HTTP %s", cover_url, r.status_code)
                return None
            data = r.content
            if not data or len(data) < 200:
                log.warning("cover download %s → suspiciously small (%d bytes)",
                            cover_url, len(data))
                return None
            return data
    except Exception as e:  # noqa: BLE001
        log.warning("cover download %s → exception: %s", cover_url, e)
        return None


def _guess_extension(cover_url: str) -> str:
    """Pick a Telegram-friendly filename extension from the CDN URL."""
    u = (cover_url or "").lower()
    for ext in (".webp", ".png", ".gif", ".jpg", ".jpeg"):
        if ext in u:
            return ext if ext != ".jpeg" else ".jpg"
    return ".jpg"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def post_cover(
    client: TelegramClient,
    url: str,
    *,
    channel_id: Optional[int] = None,
    requester_handle: Optional[str] = None,
) -> Optional[CoverPost]:
    """Scrape the gallery + post its cover to the database channel.

    Returns a `CoverPost` on success (or on cover-download failure — in which
    case the text-only fallback message is posted and marked via
    `used_fallback_text_only=True`).

    Returns None if:
      - hf_scraper couldn't return metadata (invalid URL / gallery deleted),
      - the channel_id resolution failed,
      - Telegram refused the send for a non-flood reason.
    """
    if channel_id is None:
        channel_id = int(settings.database_channel_id)

    # 1) scrape ------------------------------------------------------------
    try:
        meta = await hf_scraper.fetch_gallery_meta(url)
    except Exception as e:  # noqa: BLE001
        log.exception("hf_scraper.fetch_gallery_meta raised for %s: %s", url, e)
        meta = None

    if meta is None or not (getattr(meta, "title", None) or getattr(meta, "gallery_id", None)):
        log.warning("cover_poster: no metadata for %s", url)
        return None

    title  = str(getattr(meta, "title", "") or "")
    tags_v = getattr(meta, "tags", []) or []
    tags: List[str] = []
    for t in tags_v:
        if isinstance(t, dict):
            n = t.get("name") or ""
            if n: tags.append(str(n))
        elif t:
            tags.append(str(t))
    pages     = getattr(meta, "pages", None)
    cover_url = getattr(meta, "cover_url", None)

    caption = _format_caption(title, tags, pages, url, requester_handle)

    # 2) download cover ---------------------------------------------------
    cover_bytes = await _download_cover(cover_url) if cover_url else None

    # 3) post to the DB channel -------------------------------------------
    try:
        if cover_bytes:
            buf = io.BytesIO(cover_bytes)
            buf.name = f"cover_{getattr(meta, 'gallery_id', 'x')}{_guess_extension(cover_url or '')}"
            sent = await client.send_file(
                channel_id,
                file=buf,
                caption=caption,
                force_document=False,   # send as photo, not doc
            )
            used_fallback = False
        else:
            # Text-only fallback: the PDF still needs something to reply to.
            sent = await client.send_message(channel_id, caption)
            used_fallback = True
    except FloodWaitError:
        # Bubble up so the caller's flood-wait wrapper can decide.
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("cover_poster: send_file/send_message failed: %s", e)
        return None

    msg_id = int(getattr(sent, "id", 0))
    open_link = build_open_link(channel_id, msg_id) if msg_id else ""

    return CoverPost(
        msg_id=msg_id,
        channel_id=int(channel_id),
        open_link=open_link,
        title=title,
        pages=int(pages) if pages else None,
        tags=tags,
        cover_url=cover_url,
        used_fallback_text_only=used_fallback,
    )


async def delete_cover(
    client: TelegramClient,
    *,
    channel_id: int,
    msg_id: int,
) -> bool:
    """Delete a previously posted cover. Used on FAILED_BOT2_ERROR.

    Returns True if the deletion call succeeded (or the message was already
    gone); False on a real failure.
    """
    if not msg_id or not channel_id:
        return False
    try:
        await client.delete_messages(int(channel_id), [int(msg_id)])
        return True
    except MessageDeleteForbiddenError:
        log.warning("cannot delete cover msg %s (userbot lacks permission)", msg_id)
        return False
    except Exception as e:  # noqa: BLE001
        log.warning("delete_cover(%s) failed: %s", msg_id, e)
        return False
