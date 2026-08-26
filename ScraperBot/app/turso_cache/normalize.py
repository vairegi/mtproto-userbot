"""
normalize.py — v12.47 canonical Turso payload layer (PURE: zero I/O).

One payload schema for the shared nhentai_cache table, enforced at WRITE
time in every bot:

  gallery:<id>  -> canonical detail dict (id str, title str, cover full URL,
                   pages int, favorites int, upload_date str, scanlator str,
                   tags [{name,type}], tag_groups {type: [names]})
  search:*      -> list of card dicts (id str, title str, title_en_clean str,
                   cover full URL, pages int, tags [])
  everything else (suggest:, trending:, bm:cover:, bot2 state, ...) passes
  through untouched — only gallery:/search: carry canonical schemas.

Why this exists: the same gallery:<id> row used to be written by THREE
different code paths with three different shapes (pages as int / str /
list / dict, cover as URL / CDN-path / {"t":"j"} dict, title as str /
{"english":...} dict). Bot2Fetcher's meta.py had to grow a coercion layer
(v12.44) to survive it. This module is the write-side fix: refuse or
converge BEFORE the row lands in Turso.

Refusal contract: normalize_for_write() returns (False, None) and logs a
WARNING carrying the source bot, the key, the gallery id and the exact
field that failed — the cache is never poisoned by a write.

This file is the single source of truth. Byte-identical copies live at
  * common/turso_cache/normalize.py            (BOT 0 — repo-root deploy)
  * ScraperBot/app/turso_cache/normalize.py    (BOT 1 — subtree deploy)
  * Bot2Fetcher/app/turso_cache/normalize.py   (BOT 2 — subtree deploy)
because Render runs BOT 1 / BOT 2 with their subtree as root, so a
repo-root `common` package is not importable there. tests/test_turso_cache.py
asserts all three copies stay byte-identical.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("common.turso_cache")

_T_CDN = "https://t.nhentai.net"
_I_CDN = "https://i.nhentai.net"
_NH_EXT_MAP = {"j": "jpg", "p": "png", "g": "gif", "w": "webp"}

_EVENT_PREFIX_RE = re.compile(r"^\([A-Za-z0-9+\- ]+\)\s*")
_BRACKET_TAIL_RE = re.compile(r"(\[[^\]]*\])\s*$")


# ---------------------------------------------------------------------------
# Scalar coercion (shared superset of Bot2Fetcher v12.44 _coerce_int — the
# reader-side safety net in meta.py stays untouched for legacy rows)
# ---------------------------------------------------------------------------
def coerce_int(*vals) -> int:
    """int / float / numeric str / [x, ...] / {"pages"|"value"|"count"|"n": x}
    -> int. Anything unrecoverable -> 0 (never raises)."""
    def _one(v) -> int:
        if v is None or isinstance(v, bool):
            return 0
        if isinstance(v, (int, float)):
            return int(v)
        if isinstance(v, str):
            s = v.strip()
            if s.isdigit():
                return int(s)
            try:
                return int(float(s))
            except (TypeError, ValueError):
                return 0
        if isinstance(v, (list, tuple)):
            for el in v:
                n = _one(el)
                if n:
                    return n
            return 0
        if isinstance(v, dict):
            for k in ("pages", "num_pages", "value", "count", "n"):
                if k in v:
                    n = _one(v[k])
                    if n:
                        return n
            return 0
        return 0
    for v in vals:
        n = _one(v)
        if n:
            return n
    return 0


def coerce_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, dict):  # v2 title objects
        return str(v.get("english") or v.get("pretty")
                    or v.get("japanese") or "")
    if isinstance(v, (list, tuple)):
        return coerce_str(v[0]) if v else ""
    return str(v)


# ---------------------------------------------------------------------------
# Cover URL construction (shared superset of Bot2Fetcher v12.43)
# ---------------------------------------------------------------------------
def construct_cover_url(p: Dict[str, Any]) -> str:
    """Full https://t.nhentai.net/... cover URL from any payload shape:
    string path/URL, dict with path/url, v1 {"t":"j"} dict + media_id, or a
    bare media_id guess (extension fallback happens at download time).
    "" only when nothing usable exists."""
    media_id = coerce_str(p.get("media_id")).strip()

    for field in ("thumbnail", "cover", "cover_url", "thumb_url"):
        v = p.get(field)
        if isinstance(v, str) and v.strip():
            s = v.strip()
            if s.startswith("//"):
                return "https:" + s
            if s.startswith("http"):
                return s
            return _T_CDN + "/" + s.lstrip("/")
        if isinstance(v, dict):
            for kk in ("path", "url", "src"):
                s = coerce_str(v.get(kk)).strip()
                if s:
                    if s.startswith("//"):
                        return "https:" + s
                    if s.startswith("http"):
                        return s
                    return _T_CDN + "/" + s.lstrip("/")

    if media_id:
        for field in ("cover", "thumbnail"):
            v = p.get(field)
            if isinstance(v, dict) and v:
                ext = _NH_EXT_MAP.get(
                    coerce_str(v.get("t")).strip().lower() or "j", "jpg")
                name = "cover" if field == "cover" else "thumb"
                return f"{_T_CDN}/galleries/{media_id}/{name}.{ext}"


    # Nested v2 detail shape: images.cover / images.thumbnail carry the
    # v1-style {"t": "j"} dicts (the v12.45 fetcher hoisted these; the
    # shared layer handles them directly so no caller needs to hoist).
    images = p.get("images") or {}
    if isinstance(images, dict) and media_id:
        for field in ("cover", "thumbnail"):
            v = images.get(field)
            if isinstance(v, dict) and v:
                ext = _NH_EXT_MAP.get(
                    coerce_str(v.get("t")).strip().lower() or "j", "jpg")
                name = "cover" if field == "cover" else "thumb"
                return f"{_T_CDN}/galleries/{media_id}/{name}.{ext}"

    if media_id:
        return f"{_T_CDN}/galleries/{media_id}/cover.jpg"
    return ""


def _direct_page1(item: Dict[str, Any]) -> str:
    """High-quality page-1 URL: i.nhentai.net/galleries/<media_id>/1.<ext>."""
    media_id = coerce_str(item.get("media_id")).strip()
    images = item.get("images") or {}
    pages = images.get("pages") if isinstance(images, dict) else None
    if not (media_id and isinstance(pages, list) and pages):
        return ""
    first = pages[0] if isinstance(pages[0], dict) else {}
    ext = _NH_EXT_MAP.get(coerce_str(first.get("t")).strip().lower() or "j", "jpg")
    return f"{_I_CDN}/galleries/{media_id}/1.{ext}"


# ---------------------------------------------------------------------------
# Titles / tags / dates
# ---------------------------------------------------------------------------
def clean_title(raw: str) -> str:
    s = (raw or "").strip()
    s = _EVENT_PREFIX_RE.sub("", s)
    prev = None
    while prev != s:
        prev = s
        s = _BRACKET_TAIL_RE.sub("", s).strip()
    return s or (raw or "").strip()


def _title_from_item(item: Dict[str, Any], gid: str) -> str:
    t = item.get("title")
    if isinstance(t, dict):
        s = t.get("english") or t.get("pretty") or t.get("japanese") or ""
        return coerce_str(s) or f"Gallery {gid}"
    for k in ("title", "english_title", "title_english", "japanese_title"):
        s = coerce_str(item.get(k)).strip()
        if s:
            return s
    return f"Gallery {gid}"


def _flat_tags(item: Dict[str, Any]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for t in (item.get("tags") or []):
        if isinstance(t, dict):
            nm = coerce_str(t.get("name")).strip()
            typ = coerce_str(t.get("type")).strip().lower() or "tag"
        else:
            nm = coerce_str(t).strip()
            typ = "tag"
        if nm:
            out.append({"name": nm, "type": typ})
    return out


def _group_tags(flat: List[Dict[str, str]]) -> Dict[str, List[str]]:
    groups: Dict[str, List[str]] = {}
    for t in flat:
        groups.setdefault(t["type"], []).append(t["name"])
    return groups


def _iso_date(v: Any) -> str:
    import datetime as _dt
    if isinstance(v, _dt.datetime):
        return v.isoformat()
    if isinstance(v, (int, float)) and v:
        try:
            return _dt.datetime.fromtimestamp(float(v), _dt.timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""
    return coerce_str(v).strip()


# ---------------------------------------------------------------------------
# Canonical payload builders
# ---------------------------------------------------------------------------
def normalize_gallery_payload(raw: Any, gid_hint: str = ""
                              ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Any gallery payload shape -> canonical detail dict.
    Returns (payload, None) or (None, "field=<what failed>")."""
    if not isinstance(raw, dict):
        return None, "field=payload (not a dict)"

    gid = coerce_str(raw.get("id") or raw.get("gallery_id") or gid_hint).strip()
    if not gid:
        return None, "field=id (missing or empty)"

    # Idempotent: already-canonical rows pass through with id/pages coerced.
    if isinstance(raw.get("tag_groups"), dict) and isinstance(raw.get("title"), str):
        out = dict(raw)
        out["id"] = str(raw.get("id"))
        out["pages"] = coerce_int(raw.get("pages"), raw.get("num_pages"))
        return out, None

    title = _title_from_item(raw, gid)
    flat = _flat_tags(raw)
    groups = _group_tags(flat)
    cover = construct_cover_url(raw)
    if not cover:
        return None, ("field=cover (no usable cover/thumbnail and no "
                      "media_id to construct one from)")
    page1 = _direct_page1(raw)

    return {
        "id":             gid,
        "title":          title,
        "title_english":  coerce_str(raw.get("title_english")
                                     or (raw.get("title") or {}).get("english")
                                     if isinstance(raw.get("title"), dict)
                                     else raw.get("title_english")),
        "title_japanese": coerce_str(raw.get("title_japanese")
                                     or (raw.get("title") or {}).get("japanese")
                                     if isinstance(raw.get("title"), dict)
                                     else raw.get("title_japanese")),
        "cover":          cover,
        "page1_url":      page1,
        "pages":          coerce_int(raw.get("pages"), raw.get("num_pages")),
        "favorites":      coerce_int(raw.get("favorites"), raw.get("num_favorites")),
        "upload_date":    _iso_date(raw.get("upload_date")),
        "scanlator":      coerce_str(raw.get("scanlator")),
        "tags":           flat,
        "tag_groups":     groups,
    }, None


def _card_from_v2(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    gid = coerce_str(item.get("id")).strip()
    if not gid:
        return None
    cover = construct_cover_url(item)
    if not cover:
        return None
    title = _title_from_item(item, gid)
    return {
        "id":             gid,
        "title":          title,
        "title_en_clean": clean_title(title),
        "cover":          cover,
        "pages":          coerce_int(item.get("pages"), item.get("num_pages")),
        "tags":           [],
    }


def normalize_search_payload(raw: Any) -> Tuple[Any, Optional[str]]:
    """Search-page payload -> canonical list of card dicts.

    list                        -> cards coerced in place (id str, pages int,
                                   cover str); undictable entries dropped
    dict with "result" list     -> raw v2 page -> canonical cards
    dict without "result"       -> PASSTHROUGH (unrecognised but not
                                   provably bad — never block a real write)
    None                        -> refused (would write a literal null)
    Returns (payload, None) or (None, reason)."""
    if raw is None:
        return None, "field=payload (None)"
    if isinstance(raw, list):
        out: List[Dict[str, Any]] = []
        for el in raw:
            if not isinstance(el, dict):
                continue
            gid = coerce_str(el.get("id")).strip()
            if not gid:
                continue
            card = dict(el)
            card["id"] = gid
            card["title"] = coerce_str(card.get("title")) or f"Gallery {gid}"
            card["cover"] = coerce_str(card.get("cover")) or construct_cover_url(el)
            card["pages"] = coerce_int(card.get("pages"), card.get("num_pages"))
            if not isinstance(card.get("tags"), list):
                card["tags"] = []
            out.append(card)
        return out, None
    if isinstance(raw, dict):
        result = raw.get("result")
        if isinstance(result, list):
            cards = [c for c in (_card_from_v2(it) for it in result) if c]
            return cards, None
        return raw, None   # passthrough — unrecognised dict shape
    return raw, None       # scalar/legacy — passthrough


def normalize_for_write(key: Any, payload: Any, source: str = "unknown"
                        ) -> Tuple[bool, Any]:
    """THE write-time gate. Every bot calls this before inserting into
    nhentai_cache. Returns (ok, payload_to_write). On (False, None) the
    caller MUST NOT write — and a loud WARNING has already been logged with
    the source bot, key, gallery id and failing field."""
    k = str(key or "")
    if k.startswith("gallery:"):
        gid = k.split(":", 1)[1]
        out, err = normalize_gallery_payload(payload, gid_hint=gid)
        if out is None:
            log.warning("🚫 turso_cache REFUSE write [source=%s] key=%s "
                        "gallery=%s reason=%s", source, k, gid, err)
            return False, None
        return True, out
    if k.startswith("search:"):
        out, err = normalize_search_payload(payload)
        if out is None:
            log.warning("🚫 turso_cache REFUSE write [source=%s] key=%s "
                        "reason=%s", source, k, err)
            return False, None
        return True, out
    # suggest:, trending:, bm:cover:, private tables — generic blobs,
    # untouched by design.
    return True, payload
