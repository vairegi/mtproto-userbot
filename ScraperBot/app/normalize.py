"""
normalize.py — v1.16: convert raw nhentai API payloads into the EXACT
normalized shapes BOT 0 reads.

Why this exists (the bug it fixes):
  BOT 0's read paths only accept their own normalized shapes:
    * scraper_bridge search read:  `if isinstance(_hit, list): return _hit`
      -> the stored value for `search:*` keys MUST be a list of card dicts.
    * scraper_bridge detail read:  `if isinstance(_hit, dict) and _hit.get("id")`
      with normalized keys (title=string, tag_groups, page1_url, ...).
  BOT 1 (pre-v1.16) stored RAW nhentai JSON:
    * search page  -> {"result": [...], "num_pages": N}   (a DICT, not a list)
    * gallery      -> {"title": {"english": ...}, "tags": [...], ...}
  Result: BOT 0 rejected every BOT-1-written row as a cache MISS (search)
  and the frontend rendered `[object Object]` on the detail sheet.

These helpers are byte-for-byte ports of BOT 0's:
  miniapp/backend/app/services/scraper_bridge.py
    _normalize / _title_from_item / _title_en_clean_from_item /
    _thumb_url_from_item / _direct_nhentai_page1 / _group_tags / _iso_date /
    clean_title / the English-only tag_id=12227 filter.

No network, no DB, no async — pure functions so they're trivially testable.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_T_CDN = "https://t.nhentai.net"
_NH_EXT_MAP = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}

# nhentai's stable language tag id for English (verified by BOT 0).
_ENGLISH_TAG_ID = 12227

_EVENT_PREFIX = re.compile(r"^\([A-Za-z0-9+\- ]+\)\s*")
_BRACKET_TAIL = re.compile(r"(\[[^\]]*\])\s*$")


# ---------------------------------------------------------------------------
# Title helpers (ported from BOT 0)
# ---------------------------------------------------------------------------
def clean_title(raw: str) -> str:
    """Strip leading event tags '(C92)' and trailing meta brackets
    '[English] [Scans]' from a title."""
    s = (raw or "").strip()
    s = _EVENT_PREFIX.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _BRACKET_TAIL.sub("", s).strip()
    return s or (raw or "").strip()


def _title_from_item(item: dict) -> str:
    """v2 search rows expose english_title / japanese_title (plain strings).
    Older v1 rows exposed a title dict; handle both."""
    et = item.get("english_title")
    if isinstance(et, str) and et.strip():
        return clean_title(et)
    jt = item.get("japanese_title")
    if isinstance(jt, str) and jt.strip():
        return clean_title(jt)
    t = item.get("title")
    if isinstance(t, dict):
        return clean_title(t.get("english") or t.get("pretty") or t.get("japanese") or "")
    if isinstance(t, str):
        return clean_title(t)
    return ""


def _title_en_clean_from_item(item: dict) -> str:
    """Cleaned English title for the card GRID (title.english first)."""
    t = item.get("title")
    if isinstance(t, dict):
        et = t.get("english")
        if isinstance(et, str) and et.strip():
            return clean_title(et)
    et = item.get("english_title")
    if isinstance(et, str) and et.strip():
        return clean_title(et)
    return _title_from_item(item)


def _thumb_url_from_item(item: dict) -> str:
    """Build the cover/thumbnail URL from a v2 search row's CDN-relative
    `thumbnail` path; fall back to the legacy v1 media_id/images shape."""
    thumb = item.get("thumbnail")
    if isinstance(thumb, str) and thumb.strip():
        return _T_CDN + "/" + thumb.strip().lstrip("/")
    media_id = item.get("media_id") or ""
    images = item.get("images") or {}
    cover = images.get("cover") or images.get("thumbnail") or {}
    ext = _NH_EXT_MAP.get(cover.get("t", "j"), "jpg")
    return f"{_T_CDN}/galleries/{media_id}/cover.{ext}"


# ---------------------------------------------------------------------------
# Search page -> list of normalized cards (what BOT 0's `isinstance(list)`
# read expects). Mirrors the `out` list BOT 0 builds in _direct_nhentai_search.
# ---------------------------------------------------------------------------
def _is_english(item: dict) -> bool:
    """English-only filter matching BOT 0's hf_scraper.search()."""
    tag_ids = item.get("tag_ids") or []
    try:
        return _ENGLISH_TAG_ID in tag_ids
    except TypeError:
        return False


def _card_from_item(item: dict) -> Optional[Dict[str, Any]]:
    """One v2 search row -> one normalized card dict (BOT 0's `out` shape).

    BOT 0's scraper_bridge `out` rows carry: id, title, title_en_clean,
    cover, pages, tags. `_normalize` downstream only needs id/title/cover/
    pages/tags but we include title_en_clean so the card grid caption is
    identical to a BOT-0-native row.
    """
    gid = item.get("id")
    if gid is None:
        return None
    if not _is_english(item):
        return None
    title = _title_from_item(item)
    title_clean = _title_en_clean_from_item(item)
    cover = _thumb_url_from_item(item)
    # tags as {name,type} dicts — v2 search rows have only numeric tag_ids,
    # so we emit an empty tag list here (BOT 0 does the same for search rows;
    # card grids don't need tag names, the detail sheet does).
    return {
        "id": str(gid),
        "title": title,
        "title_en_clean": title_clean,
        "cover": cover,
        "pages": item.get("num_pages"),
        "tags": [],
    }


def normalize_search_page(raw: Any) -> List[Dict[str, Any]]:
    """Raw search payload -> list of normalized cards.

    Accepts either the raw nhentai dict ({"result": [...]}) or an
    already-normalized list (idempotent — returns it unchanged). This makes
    the re-normalize admin pass safe to run over mixed-shape rows.
    """
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, dict):
        return []
    result = raw.get("result") or []
    out: List[Dict[str, Any]] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        card = _card_from_item(item)
        if card is not None:
            out.append(card)
    return out


# ---------------------------------------------------------------------------
# Gallery detail -> normalized detail dict (what BOT 0's
# `_direct_nhentai_detail` caches and the detail sheet renders).
# ---------------------------------------------------------------------------
def _iso_date(ts: Any) -> str:
    try:
        import datetime
        return datetime.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _group_tags(item: dict) -> Dict[str, List[str]]:
    """Group v2 detail tags by type: artist / parody / character / group /
    tag / language / category."""
    groups: Dict[str, List[str]] = {}
    for t in item.get("tags") or []:
        if not isinstance(t, dict):
            continue
        typ = str(t.get("type") or "tag")
        nm = str(t.get("name") or "").strip()
        if not nm:
            continue
        groups.setdefault(typ, []).append(nm)
    return groups


def _direct_page1(item: dict) -> str:
    """High-quality page-1 URL: i.nhentai.net/galleries/<media_id>/1.<ext>."""
    media_id = str(item.get("media_id") or "").strip()
    images = item.get("images") or {}
    pages = images.get("pages") if isinstance(images, dict) else None
    if not (media_id and isinstance(pages, list) and pages):
        return ""
    first = pages[0] if isinstance(pages[0], dict) else {}
    ext = _NH_EXT_MAP.get((first.get("t") or "j").strip().lower(), "jpg")
    return f"https://i.nhentai.net/galleries/{media_id}/1.{ext}"


def normalize_gallery(raw: Any) -> Optional[Dict[str, Any]]:
    """Raw v2 gallery JSON -> normalized detail dict (BOT 0's `_detail_out`).

    If the input is already normalized (has `tag_groups` and a string
    `title`), return it unchanged — idempotent for the re-normalize pass.
    Returns None for unusable input.
    """
    if not isinstance(raw, dict):
        return None
    # Already normalized? (BOT 0's shape has tag_groups + string title)
    if isinstance(raw.get("tag_groups"), dict) and isinstance(raw.get("title"), str):
        return raw

    item = raw
    title_obj = item.get("title") or {}
    english_full = title_obj.get("english") or "" if isinstance(title_obj, dict) else ""
    japanese_full = title_obj.get("japanese") or "" if isinstance(title_obj, dict) else ""
    pretty = (title_obj.get("pretty") or "") if isinstance(title_obj, dict) else ""

    cover_path = (item.get("cover") or {}).get("path") or ""
    cover_thumb = _T_CDN + "/" + cover_path.lstrip("/") if cover_path else _thumb_url_from_item(item)
    page1 = _direct_page1(item)
    cover = page1 or cover_thumb

    groups = _group_tags(item)
    flat_tags = [{"name": n, "type": typ} for typ, names in groups.items() for n in names]

    if item.get("id") is None:
        return None

    return {
        "id":       item.get("id"),
        "title":    clean_title(pretty) if pretty else _title_from_item(item),
        "title_english":  english_full,
        "title_japanese": japanese_full,
        "cover":    cover,
        "page1_url": page1,
        "pages":    item.get("num_pages"),
        "favorites": item.get("num_favorites"),
        "upload_date": _iso_date(item.get("upload_date")),
        "scanlator": item.get("scanlator") or "",
        "tags":     flat_tags,
        "tag_groups": groups,
    }
