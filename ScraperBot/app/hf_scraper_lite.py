"""
hf_scraper_lite.py — minimal nhentai API client for BOT 1.

We deliberately do NOT import BOT 0's hf_scraper.py — BOT 1 must be
independent (different repo folder deployed to a different Render acc).

Two endpoints only:
  GET /api/v2/search?query=<q>&sort=<s>&page=<p>
  GET /api/v2/galleries/<id>

Anon quotas (per openapi.json):
  /search       : 10/min
  /galleries/id : 20/min

We share the token bucket with BOT 0 via Mongo (`nhentai_bucket`), so the
two processes never race the quota.

v1.24 (English-only enforcement — 2026-08-30):
  Chip pages (empty query) already sent query=language:english, but TAG
  pages (trending tags + EXTRA_TAG_SORTS) sent their query verbatim —
  `tag:<slug>` with no language filter — so every tag sweep cached
  non-English galleries into Turso, which Bot 2 then downloaded as PDFs.
  Fix: when settings.english_only is on (default true), every non-empty
  query gets ` language:english` appended unless it already carries an
  explicit language filter. The TURSO CACHE KEY is computed from the
  user's raw query in list_sweeper (not from this upstream query), so
  key parity with BOT 0 is unaffected.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

log = logging.getLogger("scraperbot.scraper")

BASE_URL = "https://nhentai.net"
API_URL = f"{BASE_URL}/api/v2"

# v1.24: the English-language query token. Appending it to any /search
# query restricts results to English galleries (nhentai supports compound
# queries, e.g. "tag:incest language:english").
_ENGLISH_GUARD = "language:english"


def _english_only() -> bool:
    """Env-gated (ENGLISH_ONLY, default true). getattr-guarded so a stale
    config.py without the new field can never crash the scraper."""
    return bool(getattr(settings, "english_only", True))


def _headers() -> Dict[str, str]:
    h = {
        "User-Agent": settings.user_agent,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
        "Referer": f"{BASE_URL}/",
    }
    if settings.nhentai_api_key:
        h["Authorization"] = f"Key {settings.nhentai_api_key}"
    return h


_TIMEOUT = 20.0


class UpstreamError(Exception):
    def __init__(self, status: int, body: str = ""):
        super().__init__(f"upstream {status}")
        self.status = status
        self.body = body


class RateLimited(UpstreamError):
    def __init__(self, retry_after: Optional[int] = None):
        super().__init__(429, "")
        self.retry_after = retry_after


async def _get_json(client: httpx.AsyncClient, path: str,
                    params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{API_URL}{path}"
    try:
        r = await client.get(url, params=params, headers=_headers(), timeout=_TIMEOUT)
    except httpx.HTTPError as e:
        raise UpstreamError(0, str(e)) from e
    if r.status_code == 429:
        ra_raw = r.headers.get("Retry-After") or r.headers.get("retry-after")
        ra: Optional[int] = None
        try:
            ra = int(float(ra_raw)) if ra_raw else None
        except (TypeError, ValueError):
            ra = None
        raise RateLimited(retry_after=ra)
    if r.status_code >= 400:
        raise UpstreamError(r.status_code, r.text[:200])
    try:
        return r.json()
    except Exception as e:  # noqa: BLE001
        raise UpstreamError(r.status_code, f"non-JSON: {e!s}") from e


# ---- public API ----------------------------------------------------------
async def fetch_search_page(
    client: httpx.AsyncClient,
    *,
    query: str = "",
    sort: str = "popular",
    page: int = 1,
) -> Dict[str, Any]:
    """One listing call. Routes by query (matches BOT 0's hf_scraper.py):
        * empty query  → GET /api/v2/search?query=language:english&sort=<s>&page=<p>
          (v1.0 used /search?query=&… and got 400s; nhentai rejects an
          empty `query` string.)
        * non-empty    → GET /api/v2/search?query=<q>&sort=<s>&page=<p>
    Returns the raw JSON blob so we cache upstream verbatim."""
    q = (query or "").strip()
    params: Dict[str, Any] = {
        "sort": sort or "popular",
        "page": int(page or 1),
    }
    if q:
        # v1.24: enforce English-only on tag/typed queries. A query that
        # already carries an explicit language filter is respected as-is
        # (never double-append).
        if _english_only() and "language:" not in q.lower():
            q = f"{q} {_ENGLISH_GUARD}"
        params["query"] = q
        return await _get_json(client, "/search", params)
    # v12.33c (pagination bug fix): the empty-query path previously used
    # GET /api/v2/galleries?sort=..&page=N. That endpoint IGNORES both
    # `sort` and `page` — proven live: page=1 and page=6 return the same
    # latest-uploads feed, so every Discover page rendered identical cards.
    # /api/v2/search requires a non-empty query, so for chip pages we send
    # query=language:english (the app's English-only spirit; mirrors BOT 0's
    # scraper_bridge empty-query fallback which uses query=english) and the
    # /search endpoint which DOES honor sort + page.
    params["query"] = _ENGLISH_GUARD
    return await _get_json(client, "/search", params)


async def fetch_gallery(
    client: httpx.AsyncClient,
    gallery_id: str | int,
) -> Dict[str, Any]:
    """One /api/v2/galleries/<id> call, with related+suggestions+comments
    included so BOT 0's detail view has everything on the first read."""
    return await _get_json(
        client,
        f"/galleries/{gallery_id}",
        {"include": "related,suggestions,comments"},
    )


def extract_ids_from_search(payload: Dict[str, Any]) -> List[str]:
    """Pull gallery IDs out of a /search response. Robust to shape drift:
    walks `result` / `results` / `hits` / `data`, picks .id or .gallery_id."""
    ids: List[str] = []
    seen: set[str] = set()
    for key in ("result", "results", "hits", "data", "items"):
        arr = payload.get(key)
        if isinstance(arr, list):
            for item in arr:
                if not isinstance(item, dict):
                    continue
                gid = item.get("id") or item.get("gallery_id") or item.get("media_id")
                if gid is None:
                    continue
                s = str(gid).strip()
                if s and s not in seen:
                    seen.add(s)
                    ids.append(s)
    return ids


async def make_client() -> httpx.AsyncClient:
    """One AsyncClient per sweeper; reuse across many requests.

    v1.22.8: cap the connection pool. The default pool (100 keep-alive
    connections) let socket buffers multiply the resident set during sweep
    bursts — one of the contributors to the status-137 OOM kills on the
    512MB free instance. 4 connections is plenty for a single sweeper
    walking pages sequentially with 3-6s gaps."""
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=_TIMEOUT,
        headers=_headers(),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
    )
