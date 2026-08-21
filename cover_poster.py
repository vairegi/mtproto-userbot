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
from telethon.tl.types import InputMediaUploadedPhoto

import hf_scraper
from config import settings
# v11.2: normalise arbitrary cover bytes into a well-formed JPEG before
# uploading to Telegram — fixes the "tiny preview" bug where .webp / .gif
# covers were rejected by InputMediaUploadedPhoto with "The extension of
# the photo is invalid" and fell back to a non-photo send.
from cover_image_normalise import normalise_cover_bytes

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


@dataclass
class PreparedCover:
    """v12.34 (Task 2): everything needed to post a cover, WITHOUT having
    posted it yet.

    Built by `prepare_cover()` (scrape + caption + cover bytes), consumed
    by `post_prepared_cover()` inside the pool's channel_write() lock so
    the cover post + PDF forward land back-to-back with no other job's
    writes in between.

    `cover_bytes` is held in memory for the lifetime of the Bot 2 wait
    (~10-600s per job). Worst case is pool_size × ~1 MB ≈ 2 MB —
    negligible against the 512 MB ceiling.
    """
    caption: str
    cover_bytes: Optional[bytes]
    cover_ext: str                    # ".jpg" / ".png" / "" — post hint
    title: str
    pages: Optional[int]
    tags: List                        # typed [{'name','type'}] — same as CoverPost
    cover_url: Optional[str]
    gallery_id: Optional[str]


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

# ---------------------------------------------------------------------------
# v12.34 (Task 2): split the old post_cover into prepare + post.
# ---------------------------------------------------------------------------
# `prepare_cover` does scrape + caption + cover download WITHOUT touching
# the channel. `post_prepared_cover` does ONLY the channel send. The pair
# exists so relay_v2 can hold the channel lock around
#   post_prepared_cover(...)  +  forward_messages(pdf)
# back-to-back, guaranteeing the DB channel always reads
# cover_A, pdf_A, cover_B, pdf_B — never cover_A, cover_B, pdf_A, pdf_B.
#
# `post_cover` (legacy single-call) is kept as a thin wrapper so any old
# call site continues to work byte-identically to v12.33.
# ---------------------------------------------------------------------------

async def prepare_cover(
    url: str,
    *,
    requester_handle: Optional[str] = None,
) -> Optional[PreparedCover]:
    """Scrape the gallery + build the caption + download the cover bytes.

    NO channel write. Returns a PreparedCover on success, None if the
    scrape returned nothing usable (invalid URL / gallery deleted).
    """
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

    cover_bytes = await _download_cover(cover_url) if cover_url else None
    cover_ext = ""
    if cover_bytes:
        # v11.2 normalise step moved into prepare so the in-memory bytes
        # are already JPEG-clean by the time the channel lock is held.
        norm_bytes, norm_ext = normalise_cover_bytes(
            cover_bytes, source_url=cover_url or "",
        )
        cover_bytes = norm_bytes if norm_bytes else cover_bytes
        cover_ext = norm_ext or _guess_extension(cover_url or "")

    return PreparedCover(
        caption=caption,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        title=title,
        pages=int(pages) if pages else None,
        tags=tags_typed,
        cover_url=cover_url,
        gallery_id=str(gid) if gid is not None else None,
    )


async def post_prepared_cover(
    client: TelegramClient,
    prepared: PreparedCover,
    *,
    channel_id: Optional[int] = None,
) -> Optional[CoverPost]:
    """Post a PreparedCover into the DB channel. MUST be called under the
    pool's channel_write() lock by v12.34 callers.

    Returns a CoverPost on success, None on a non-flood send failure.
    FloodWaitError bubbles up unchanged (caller's _with_flood handles it).
    """
    if channel_id is None:
        channel_id = int(settings.database_channel_id)

    gid_slug = prepared.gallery_id or "x"
    try:
        if prepared.cover_bytes:
            buf = io.BytesIO(prepared.cover_bytes)
            buf.name = f"cover_{gid_slug}{prepared.cover_ext or '.jpg'}"
            try:
                uploaded = await client.upload_file(buf, file_name=buf.name)
                spoiler_media = InputMediaUploadedPhoto(
                    file=uploaded, spoiler=True,
                )
                sent = await client.send_file(
                    channel_id,
                    file=spoiler_media,
                    caption=prepared.caption,
                    force_document=False,
                )
            except Exception as _spoiler_err:  # noqa: BLE001
                log.warning(
                    "cover_poster: spoiler upload failed, falling back to "
                    "non-spoiler send: %s", _spoiler_err,
                )
                buf.seek(0)
                sent = await client.send_file(
                    channel_id,
                    file=buf,
                    caption=prepared.caption,
                    force_document=False,
                )
            used_fallback = False
        else:
            sent = await client.send_message(channel_id, prepared.caption)
            used_fallback = True
    except FloodWaitError:
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
        title=prepared.title,
        pages=prepared.pages,
        tags=prepared.tags,
        cover_url=prepared.cover_url,
        used_fallback_text_only=used_fallback,
    )


async def post_cover(
    client: TelegramClient,
    url: str,
    *,
    channel_id: Optional[int] = None,
    requester_handle: Optional[str] = None,
) -> Optional[CoverPost]:
    """v12.34 legacy wrapper: prepare + post in one call.

    Kept so any old call site keeps working byte-identically to v12.33.
    New code in relay_v2.py uses prepare_cover + post_prepared_cover
    directly so the channel write can sit inside the pool's lock.
    """
    prepared = await prepare_cover(url, requester_handle=requester_handle)
    if prepared is None:
        return None
    return await post_prepared_cover(client, prepared, channel_id=channel_id)


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
