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
    """Result of a successful cover-post op.

    NOTE: `tags` is now List[dict] with {'name','type'} entries (matches
    hf_scraper.GalleryMeta.tags). Callers that only need names should use
    `[t['name'] for t in cover.tags]`.
    """
    msg_id: int
    channel_id: int
    open_link: str
    title: str
    pages: Optional[int]
    tags: List           # List[Dict[str, str]] but kept loose for back-compat
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


# Title-cleaning regexes (BUG 2 fix). These strip the leading event tag
# '(C92)' and the trailing meta brackets '[English] [Scans]' so the caption
# opens with a clean bold title.
_EVENT_PREFIX_RE = re.compile(r"^\([A-Za-z0-9+\- ]+\)\s*")
_BRACKET_TAIL_RE = re.compile(r"(\[[^\]]*\])\s*$")


def _clean_title(raw: str) -> str:
    """Strip leading '(C92)' event tags and trailing '[English] [Scans]'
    metadata brackets from an nhentai title."""
    s = (raw or "").strip()
    s = _EVENT_PREFIX_RE.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _BRACKET_TAIL_RE.sub("", s).strip()
    return s or (raw or "").strip()


# BUG 2 fix — tag types we emit as their own labelled row, in this exact
# order. Anything with type=='tag' (the plain flat tag list) goes into the
# trailing '➤ Tags:' row.
_META_ROW_ORDER = [
    ("group",     "Groups"),
    ("parody",    "Parodies"),
    ("artist",    "Artists"),
    ("character", "Characters"),
    ("language",  "Languages"),
    ("category",  "Categories"),
]
_TAGS_ROW_MAX = 600   # cap the '➤ Tags:' hashtag row (Telegram caption is 1024)


def _group_tags_by_type(tags) -> dict:
    """Group a flat list of {'name','type'} dicts (or plain strings) by tag
    type. Plain strings fall into the 'tag' bucket."""
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


def _format_caption(
    title: str,
    tags,
    pages: Optional[int],
    url: str,
    requester_handle: Optional[str] = None,
    gallery_id: Optional[str] = None,
) -> str:
    """Build the cover-post caption in the exact screenshot format:

        Kakkou no Su | The Cuckoo

        ➤ #295679

        ➤ Groups:     #Community #Taxonomy ...
        ➤ Parodies:   #original
        ➤ Artists:    #nakamura_regura
        ➤ Languages:  #translated #english
        ➤ Categories: #doujinshi

        ➤ Tags:       #big_breasts #sole_female ...

    Rules (BUG 2):
      * No nhentai.net URL anywhere.
      * Clean bold title (event prefix + trailing brackets stripped).
      * '➤ #<gallery_id>' line, blank line above and below.
      * Meta rows in the order Groups → Parodies → Artists → Characters
        → Languages → Categories. Rows with no tags of that type are
        omitted entirely.
      * Trailing '➤ Tags: ...' row from the flat 'tag'-type tags,
        capped at ~600 chars.
      * Hashtags use _hashtagify: spaces → underscores, punctuation stripped.
    """
    lines: List[str] = []

    # --- Clean, bold title ------------------------------------------------
    clean = _clean_title(title) if title else "(untitled)"
    # Telegram supports HTML parse-mode; but the userbot may be sending
    # plain text — emit as-is (bold visually via clear formatting). If the
    # caller wants HTML bold they can wrap the string.
    lines.append(f"**{clean}**")

    # --- ➤ #<gallery_id> --------------------------------------------------
    if gallery_id:
        gid_str = str(gallery_id).strip().lstrip("#")
        if gid_str:
            lines.append("")
            lines.append(f"➤ #{gid_str}")

    # --- Grouped metadata rows -------------------------------------------
    groups = _group_tags_by_type(tags)
    meta_lines: List[str] = []
    # Longest label determines column width ("Categories:" == 11 chars).
    label_width = max(len(lbl) for _, lbl in _META_ROW_ORDER) + 1  # +1 for ':'
    for key, label in _META_ROW_ORDER:
        names = groups.get(key) or []
        if not names:
            continue
        hashtags = [h for h in (_hashtagify(n) for n in names) if h]
        if not hashtags:
            continue
        # Left-align 'Groups:', 'Parodies:', ... so hashtags line up like
        # the screenshot's column.
        col = (label + ":").ljust(label_width)
        meta_lines.append(f"➤ {col} {' '.join(hashtags)}")

    if meta_lines:
        lines.append("")
        lines.extend(meta_lines)

    # --- Trailing ➤ Tags: row -------------------------------------------
    plain_tags = groups.get("tag") or []
    if plain_tags:
        hashtags = [h for h in (_hashtagify(n) for n in plain_tags) if h]
        if hashtags:
            joined = " ".join(hashtags)
            # Cap the tags row before assembly (Telegram caption ≤ 1024).
            if len(joined) > _TAGS_ROW_MAX:
                joined = joined[:_TAGS_ROW_MAX].rsplit(" ", 1)[0]
            col = ("Tags:").ljust(label_width)
            lines.append("")
            lines.append(f"➤ {col} {joined}")

    # --- Optional 'requested by' line (kept, but at the very end) --------
    if requester_handle:
        h = requester_handle if requester_handle.startswith("@") else f"@{requester_handle}"
        lines.append("")
        lines.append(f"requested by {h}")

    # NOTE: BUG 2 explicitly forbids emitting the nhentai URL, so `url` is
    # intentionally ignored here. The parameter is kept for call-site
    # compatibility.
    _ = url

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
    # BUG FIX: hf_scraper now returns TYPED tags ({'name','type'} dicts).
    # Preserve that shape end-to-end so:
    #   - _format_caption can build grouped rows (Groups/Parodies/Artists/…)
    #   - relay_v2 can persist typed tags on `galleries[gid].tags` so the
    #     mini-app can rebuild the same caption for the DM forward.
    tags_v = getattr(meta, "tags", []) or []
    tags_typed: List = []
    for t in tags_v:
        if isinstance(t, dict):
            n = t.get("name") or ""
            if n:
                tags_typed.append({
                    "name": str(n),
                    "type": str(t.get("type") or "tag"),
                })
        elif t:
            tags_typed.append({"name": str(t), "type": "tag"})
    pages     = getattr(meta, "pages", None)
    cover_url = getattr(meta, "cover_url", None)
    gid       = getattr(meta, "gallery_id", None)

    caption = _format_caption(
        title, tags_typed, pages, url, requester_handle, gallery_id=gid,
    )
    # Return the tags on CoverPost in the SAME typed dict shape
    # (relay_v2 now persists them as-is to Mongo).
    tags = tags_typed

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
