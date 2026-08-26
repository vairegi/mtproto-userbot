"""
similar_sql.py — v12.50 SQL-level "Similar to this" engine (Turso-only).

Replaces the v12.34k payload scan (SELECT key, payload LIMIT 2000 -> score
in Python) which both (a) loaded ~2k full payloads into Render RAM per
cold call and (b) silently returned [] through the pre-v12.46 broken
libsql driver, which is why the row never rendered.

Design constraints (Ryan's spec):
  * Ultra RAM-efficient on Render — all scoring/filtering/limiting happens
    INSIDE the SQL. Python receives at most `limit` small rows.
  * SELECT only the fields cards render: id, title, cover, pages,
    favorites (+ score for debugging/telemetry).
  * Weighted points: +10 artist/parody/group/character, +2 content tags.
  * Exclusions: current gallery, and generic metadata noise
    (language / category buckets are never scored because they live in
    separate tag_groups keys; known filler tags like "translated" are
    stripped from the target's signal list before query construction).
  * Fallback ladder: Stage A (high-tier prefilter) -> Stage B (content-tag
    prefilter) -> Stage C (same artist/category by favorites).

Requires canonical payloads (v12.49 migration): tag_groups is a JSON
object of {type: [names]} on every gallery:* row. json1 (json_extract /
json_each) is available on Turso's SQLite build — verified live
2026-08-26 against the production DB.

All values are bound parameters. LIKE patterns are ESCAPE'd. Nothing is
ever interpolated into the SQL text except the literal bucket path names
and the LIMIT integers (both validated ints).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("miniapp.similar_sql")

# Filler tags that carry no recommendation signal even though they live in
# the content-tag bucket on nhentai.
_NOISE_TAGS = frozenset({
    # strictly metadata noise — content-signal tags (full color, webtoon,
    # censorship variants, ...) stay, they carry real recommendation signal
    "translated", "rewrite", "extraneous ads", "sample", "incomplete",
    "missing cover", "replaced", "scanmark", "watermarked",
})

HIGH_TIERS = ("artist", "parody", "group", "character")   # +10 each
CONTENT_TIER = "tag"                                       # +2 each

_STAGE_A_PREFILTER_CAP = 4000   # LIKE-matched candidates before json scoring
_STAGE_B_PREFILTER_CAP = 4000
_STAGE_C_CAP = 200
MIN_RESULTS = 4                 # below this, fall through to the next stage


def _like_escape(s: str) -> str:
    r"""Escape %, _ and \ for a LIKE ... ESCAPE '\' pattern."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _signals(payload: Dict[str, Any]) -> Dict[str, List[str]]:
    """Extract the target's signal sets from a canonical payload.
    Returns {tier: [names]} with noise stripped from the content tier and
    every name lowercased + stripped. Buckets that don't exist on the row
    (e.g. no character) simply come back empty."""
    tg = payload.get("tag_groups") or {}
    if not isinstance(tg, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for tier in HIGH_TIERS + (CONTENT_TIER,):
        names = tg.get(tier)
        if not isinstance(names, list):
            out[tier] = []
            continue
        clean = []
        for n in names:
            s = str(n or "").strip().lower()
            if not s:
                continue
            if tier == CONTENT_TIER and s in _NOISE_TAGS:
                continue
            clean.append(s)
        out[tier] = clean
    return out


def _in_clause(values: List[str]) -> Tuple[str, List[str]]:
    """(' IN (?,?,?)', [values...]) — never called with an empty list."""
    return " IN (" + ",".join("?" for _ in values) + ")", list(values)


def _score_expr(sig: Dict[str, List[str]], args: List[str],
                content_terms: Optional[List[str]] = None) -> str:
    """Build the weighted score expression, appending bound values to args.

    Per tier: (SELECT COUNT(*) FROM json_each(json_extract(payload, path))
               WHERE lower(value) IN (...)) — json_each on a missing/empty
    array yields zero rows, so absent buckets cost nothing."""
    parts: List[str] = []
    high_terms: List[str] = []
    for tier in HIGH_TIERS:
        names = sig.get(tier) or []
        if not names:
            continue
        clause, vals = _in_clause(names)
        # "group" is a reserved word — json1 paths quote it.
        path = f"$.tag_groups.{tier}" if tier != "group" else '$.tag_groups."group"'
        parts.append(
            f"(SELECT COUNT(*) FROM json_each(json_extract(payload, '{path}')) "
            f"WHERE lower(value){clause})")
        args.extend(vals)
        high_terms.extend(names)
    expr10 = " + ".join(parts) if parts else "0"

    terms = content_terms if content_terms is not None else (sig.get(CONTENT_TIER) or [])
    if terms:
        clause, vals = _in_clause(terms)
        expr2 = (f"(SELECT COUNT(*) FROM json_each("
                 f"json_extract(payload, '$.tag_groups.tag')) "
                 f"WHERE lower(value){clause})")
        args.extend(vals)
    else:
        expr2 = "0"

    return f"(10 * ({expr10}) + 2 * ({expr2}))"


def _prefilter_sql(terms: List[str], args: List[str],
                   cap: int) -> str:
    """(payload LIKE ? ESCAPE '\\' OR ...) LIMIT <cap> subquery — skips
    json scoring for rows that can't possibly match."""
    likes = " OR ".join("payload LIKE ? ESCAPE '\\'" for _ in terms)
    for t in terms:
        args.append(f"%{_like_escape(t)}%")
    return (f"SELECT key, payload FROM nhentai_cache "
            f"WHERE key LIKE 'gallery:%' AND key != ? AND ({likes}) "
            f"LIMIT {int(cap)}")


def _scored_select() -> str:
    """Card fields only — nothing else crosses into Python."""
    return (
        "SELECT json_extract(payload, '$.id')        AS id, "
        "       json_extract(payload, '$.title')     AS title, "
        "       json_extract(payload, '$.cover')     AS cover, "
        "       json_extract(payload, '$.pages')     AS pages, "
        "       json_extract(payload, '$.favorites') AS favorites ")


def build_stage_a(gid: str, sig: Dict[str, List[str]],
                  limit: int) -> Tuple[Optional[str], List[Any]]:
    """High-tier-prefiltered weighted scoring. None when the target has no
    high-tier signals at all (caller should jump straight to Stage B)."""
    high_terms = [n for tier in HIGH_TIERS for n in (sig.get(tier) or [])]
    if not high_terms:
        return None, []
    args: List[Any] = [f"gallery:{gid}"]
    # ORDER MATTERS: the prefilter CTE is textually first in the SQL, so
    # its LIKE args must be bound before the score expression's tier args.
    prefilter = _prefilter_sql(high_terms[:6], args, _STAGE_A_PREFILTER_CAP)
    score = _score_expr(sig, args)
    sql = (
        f"WITH cand AS ({prefilter}), "
        f"scored AS (SELECT key, {_scored_select().replace('SELECT ', '', 1)}, "
        f"{score} AS score FROM cand) "
        f"SELECT id, title, cover, pages, favorites, score FROM scored "
        f"WHERE score > 0 "
        f"ORDER BY score DESC, CAST(favorites AS INTEGER) DESC "
        f"LIMIT {int(limit)}")
    return sql, args


def build_stage_b(gid: str, sig: Dict[str, List[str]],
                  exclude_ids: List[str], limit: int) -> Tuple[Optional[str], List[Any]]:
    """Content-tag prefilter fallback — same scoring, wider net. Excludes
    IDs Stage A already returned."""
    terms = (sig.get(CONTENT_TIER) or [])[:8]
    if not terms:
        return None, []
    args: List[Any] = [f"gallery:{gid}"]
    # same ordering rule as stage A: prefilter args first
    prefilter = _prefilter_sql(terms, args, _STAGE_B_PREFILTER_CAP)
    score = _score_expr(sig, args)
    not_in = ""
    if exclude_ids:
        marks = ",".join("?" for _ in exclude_ids)
        not_in = f" AND json_extract(payload, '$.id') NOT IN ({marks})"
    sql = (
        f"WITH cand AS ({prefilter}), "
        f"scored AS (SELECT key, {_scored_select().replace('SELECT ', '', 1)}, "
        f"{score} AS score FROM cand) "
        f"SELECT id, title, cover, pages, favorites, score FROM scored "
        f"WHERE score > 0{not_in} "
        f"ORDER BY score DESC, CAST(favorites AS INTEGER) DESC "
        f"LIMIT {int(limit)}")
    args.extend(exclude_ids)
    return sql, args


def build_stage_c(gid: str, payload: Dict[str, Any],
                  exclude_ids: List[str], limit: int) -> Tuple[Optional[str], List[Any]]:
    """Popularity fallback: same artist (else same category), favorites
    DESC. Guarantees the row almost never renders empty."""
    tg = payload.get("tag_groups") or {}
    bucket, name = "", ""
    artists = tg.get("artist") if isinstance(tg, dict) else None
    cats = tg.get("category") if isinstance(tg, dict) else None
    if isinstance(artists, list) and artists:
        bucket, name = "artist", str(artists[0]).strip().lower()
    elif isinstance(cats, list) and cats:
        bucket, name = "category", str(cats[0]).strip().lower()
    if not name:
        return None, []
    path = f"$.tag_groups.{bucket}"
    args: List[Any] = [f"gallery:{gid}", name]
    not_in = ""
    if exclude_ids:
        marks = ",".join("?" for _ in exclude_ids)
        not_in = f" AND json_extract(payload, '$.id') NOT IN ({marks})"
    sql = (
        f"SELECT json_extract(payload, '$.id')        AS id, "
        f"       json_extract(payload, '$.title')     AS title, "
        f"       json_extract(payload, '$.cover')     AS cover, "
        f"       json_extract(payload, '$.pages')     AS pages, "
        f"       json_extract(payload, '$.favorites') AS favorites, "
        f"       0 AS score "
        f"FROM nhentai_cache "
        f"WHERE key LIKE 'gallery:%' AND key != ? "
        f"AND EXISTS (SELECT 1 FROM json_each(json_extract(payload, '{path}')) "
        f"            WHERE lower(value) = ?)"
        f"{not_in} "
        f"ORDER BY CAST(favorites AS INTEGER) DESC "
        f"LIMIT {int(limit)}")
    args.extend(exclude_ids)
    return sql, args


def card_from_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Row -> card dict. Defensive coercion only (rows are canonical, but
    readers must survive any straggler)."""
    gid = str(row.get("id") or "").strip()
    if not gid:
        return None
    title = row.get("title")
    if isinstance(title, dict):
        title = title.get("english") or title.get("pretty") or ""
    return {
        "id":        gid,
        "title":     str(title or f"Gallery {gid}"),
        "cover":     str(row.get("cover") or ""),
        "pages":     int(row.get("pages") or 0),
        "favorites": int(row.get("favorites") or 0),
        "tags":      [],
    }
