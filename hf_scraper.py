"""
hf_scraper.py — Direct scraper for hentaifox.com, used by:
  - /search <keyword>  (Admin Bot inline results)
  - relay.py fallback metadata (when SOURCE_API_BASE/KEY are not configured
    and Bot 1's cover post is not detected in time)

WHY THIS FILE CHANGED (2026-07 fix for HTTP 403 on Render)
----------------------------------------------------------
hentaifox.com sits behind Cloudflare with an active JavaScript / Turnstile
challenge (proven by the response headers we received during debugging:
`cf-mitigated: challenge`, `server: cloudflare`, and a `chlray` server-timing
value on EVERY request). From a datacenter IP (Render, Fly, HF Spaces, etc.)
a plain `httpx` GET is immediately answered with HTTP 403 — no gallery HTML
is ever returned, so the admin bot correctly said "Search unavailable".

We proved during a live probe that:
  * `httpx` + realistic browser headers          -> 403 (still challenged)
  * `curl_cffi` with chrome124 TLS impersonation -> 403 (still challenged)
  * `cloudscraper` (solves the JS challenge)     -> 200, real search results

So this rewrite uses `cloudscraper`. Cloudscraper is a small wrapper around
`requests` that runs Cloudflare's JS challenge with a JS engine and stores
the resulting `cf_clearance` cookie, letting subsequent requests pass through.
The old sync-only `requests` API is wrapped with `asyncio.run_in_executor`
so the rest of the async project (search_picker.py / relay.py) keeps working
without any change to its call sites.

KEY DESIGN DECISIONS
--------------------
1. ONE process-wide scraper instance is kept alive so the solved-challenge
   cookies are reused. Solving the JS challenge is expensive (~2-5s); reusing
   the session brings /search back down to ~200-400ms per page.

2. The scraper is REBUILT automatically if a request comes back 403 again
   (Cloudflare rotates challenges every 30-60 min). The rebuild is
   thread-safe: two concurrent /search commands will not race to rebuild.

3. Every request carries a full modern-Chrome header set including
   Sec-CH-UA client hints and a Referer, so Cloudflare's fingerprint check
   sees "consistent Chrome tab" rather than "headless Python".

4. Retry-with-backoff: on transient 403/5xx we retry up to 3 times with a
   jittered delay, rebuilding the scraper between attempts.

5. Nothing here raises to the caller. On terminal failure we return `None`
   so search_picker.py can show its clean "Search unavailable" message.
"""
from __future__ import annotations

import asyncio
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from bs4 import BeautifulSoup

# cloudscraper is imported lazily so this file is still importable in
# environments that haven't installed it yet (e.g. running -m py_compile).
try:
    import cloudscraper  # type: ignore
    _CLOUDSCRAPER_IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # noqa: BLE001
    cloudscraper = None  # type: ignore[assignment]
    _CLOUDSCRAPER_IMPORT_ERROR = str(_e)

from logging_setup import setup_logging

log = setup_logging("hf_scraper")

BASE_URL = "https://hentaifox.com"

# One realistic modern Chrome header block. `cloudscraper` will merge these
# with its own generated Cookie / User-Agent for the challenge phase, and
# reuse the resulting session on every subsequent call.
_BROWSER_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

_TIMEOUT = 25.0                # cloudscraper needs more headroom than httpx
_MAX_RETRIES = 3
_CHALLENGE_TTL_SEC = 25 * 60   # rebuild the session at least this often

# ------------------------------------------------------------------
# Shared scraper instance (thread-safe, lazily created and refreshed)
# ------------------------------------------------------------------
_scraper = None                # type: ignore[var-annotated]
_scraper_created_at: float = 0.0
_scraper_lock = threading.Lock()


def _build_scraper():
    """Create a fresh cloudscraper session that solves Cloudflare's challenge."""
    if cloudscraper is None:
        raise RuntimeError(
            f"cloudscraper is not installed ({_CLOUDSCRAPER_IMPORT_ERROR}). "
            f"Add `cloudscraper==1.2.71` to requirements.txt and redeploy."
        )
    s = cloudscraper.create_scraper(
        # Emulate desktop Chrome on Windows — matches the header set above.
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        # Slightly longer challenge delay: safer on slow datacenter CPUs.
        delay=6,
    )
    s.headers.update(_BROWSER_HEADERS)
    return s


def _get_scraper(force_refresh: bool = False):
    """Return the shared scraper, rebuilding it when stale or when forced."""
    global _scraper, _scraper_created_at
    now = time.time()
    with _scraper_lock:
        stale = (now - _scraper_created_at) > _CHALLENGE_TTL_SEC
        if _scraper is None or force_refresh or stale:
            if _scraper is not None:
                log.info(
                    "rebuilding cloudscraper session (force=%s stale=%s)",
                    force_refresh, stale,
                )
            _scraper = _build_scraper()
            _scraper_created_at = now
        return _scraper


# ------------------------------------------------------------------
# Data models (unchanged — callers keep working)
# ------------------------------------------------------------------

@dataclass
class SearchHit:
    gallery_id: str
    title: str
    url: str
    thumb_url: Optional[str] = None
    category: Optional[str] = None


@dataclass
class SearchPage:
    query: str
    page: int
    total_results: int
    hits: List[SearchHit] = field(default_factory=list)
    has_next: bool = False


@dataclass
class GalleryMeta:
    title: str
    tags: List[str]
    cover_url: Optional[str]
    pages: Optional[int] = None
    gallery_id: Optional[str] = None


# ------------------------------------------------------------------
# Blocking HTTP fetch (runs inside the executor)
# ------------------------------------------------------------------

def _fetch_sync(url: str, params: Optional[dict] = None,
                referer: Optional[str] = None) -> Optional[str]:
    """Blocking GET via cloudscraper, with retry + session refresh on 403."""
    headers = dict(_BROWSER_HEADERS)
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin"

    last_status: Optional[int] = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            scraper = _get_scraper(force_refresh=(attempt > 1))
            r = scraper.get(url, params=params, headers=headers,
                            timeout=_TIMEOUT, allow_redirects=True)
            last_status = r.status_code
            if r.status_code == 200 and r.text:
                return r.text
            if r.status_code == 404:
                # Real "not found" — do not retry, do not warn.
                return None
            log.warning(
                "hf_scraper HTTP %s for %s (attempt %d/%d)",
                r.status_code, url, attempt, _MAX_RETRIES,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "hf_scraper request error (attempt %d/%d): %s",
                attempt, _MAX_RETRIES, e,
            )

        # Backoff before the next attempt (jittered exponential).
        if attempt < _MAX_RETRIES:
            sleep_s = (2 ** (attempt - 1)) + random.uniform(0.5, 1.5)
            time.sleep(sleep_s)

    log.warning("hf_scraper giving up on %s after %d attempts (last=%s)",
                url, _MAX_RETRIES, last_status)
    return None


async def _get(url: str, params: Optional[dict] = None,
               referer: Optional[str] = None) -> Optional[str]:
    """Async facade so the rest of the codebase can `await` us as before."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_sync, url, params, referer)


# ------------------------------------------------------------------
# HTML parsing (unchanged from the original — only fetch layer changed)
# ------------------------------------------------------------------

def _parse_search_html(html: str, query: str, page: int) -> SearchPage:
    soup = BeautifulSoup(html, "html.parser")
    hits: List[SearchHit] = []

    header = soup.select_one("h1.tag_info")
    total = 0
    if header:
        m = re.search(r"([\d,]+)\s*results", header.get_text(" ", strip=True))
        if m:
            try:
                total = int(m.group(1).replace(",", ""))
            except ValueError:
                total = 0

    for thumb in soup.select("div.thumb"):
        a = thumb.select_one("div.inner_thumb a[href*='/gallery/']") or thumb.select_one(
            "a[href*='/gallery/']"
        )
        if not a:
            continue
        href = a.get("href") or ""
        m = re.search(r"/gallery/(\d+)/?", href)
        if not m:
            continue
        gid = m.group(1)
        title_el = thumb.select_one("h2.g_title a")
        title = title_el.get_text(strip=True) if title_el else f"Gallery {gid}"
        img = thumb.select_one("img")
        thumb_url = None
        if img is not None:
            thumb_url = img.get("data-src") or img.get("src")
            if thumb_url and thumb_url.startswith("data:"):
                thumb_url = img.get("data-src")
        cat_el = thumb.select_one("h3.g_cat a")
        category = cat_el.get_text(strip=True) if cat_el else None
        hits.append(
            SearchHit(
                gallery_id=gid,
                title=title,
                url=f"{BASE_URL}/gallery/{gid}/",
                thumb_url=thumb_url,
                category=category,
            )
        )

    # "not_found" block appears on hentaifox 404 pages (e.g. page number too high)
    not_found = soup.select_one("div.galleries_overview.not_found")
    has_next = not not_found and len(hits) > 0

    return SearchPage(query=query, page=page, total_results=total, hits=hits, has_next=has_next)


async def search(query: str, page: int = 1) -> Optional[SearchPage]:
    """Scrape https://hentaifox.com/search/?q=<query>&page=<page>.
    Returns None on network/parse failure (caller should show
    'search unavailable, try again' rather than crash)."""
    query = (query or "").strip()
    if not query:
        return None
    params = {"q": query}
    if page and page > 1:
        params["page"] = page
    html = await _get(f"{BASE_URL}/search/", params=params, referer=f"{BASE_URL}/")
    if html is None:
        return None
    try:
        return _parse_search_html(html, query, page)
    except Exception as e:  # noqa: BLE001
        log.warning("hf_scraper: failed to parse search page: %s", e)
        return None


def _parse_gallery_html(html: str, gallery_id: Optional[str] = None) -> Optional[GalleryMeta]:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one("div.info h1") or soup.select_one("h1")
    if not title_el:
        return None
    title = title_el.get_text(strip=True)

    tags: List[str] = []
    for li in soup.select("ul.tags li a.tag_btn"):
        t = li.get_text(strip=True)
        t = re.sub(r"\s*\d+\s*$", "", t).strip()  # drop trailing badge count
        if t:
            tags.append(t)

    cover_el = soup.select_one("div.cover img")
    cover_url = cover_el.get("src") if cover_el else None

    pages = None
    pages_el = soup.select_one("span.i_text.pages")
    if pages_el:
        m = re.search(r"Pages:\s*(\d+)", pages_el.get_text(" ", strip=True))
        if m:
            pages = int(m.group(1))

    gid_input = soup.select_one("input#gallery_id")
    gid = gid_input.get("value") if gid_input else gallery_id

    return GalleryMeta(title=title, tags=tags, cover_url=cover_url, pages=pages, gallery_id=gid)


async def fetch_gallery_meta(gallery_url_or_id: str) -> Optional[GalleryMeta]:
    """Fetch + parse a gallery page directly by URL or numeric ID."""
    s = (gallery_url_or_id or "").strip()
    if not s:
        return None
    if s.isdigit():
        url = f"{BASE_URL}/gallery/{s}/"
        gid = s
    else:
        m = re.search(r"/gallery/(\d+)", s)
        gid = m.group(1) if m else None
        url = s if s.startswith("http") else f"{BASE_URL}/gallery/{s}/"
    html = await _get(url, referer=f"{BASE_URL}/")
    if html is None:
        return None
    try:
        return _parse_gallery_html(html, gallery_id=gid)
    except Exception as e:  # noqa: BLE001
        log.warning("hf_scraper: failed to parse gallery page: %s", e)
        return None


# ------------------------------------------------------------------
# Health probe — used by startup_check.py or ad-hoc /diag commands.
# ------------------------------------------------------------------

async def health_check() -> bool:
    """Return True iff hentaifox.com is currently reachable and readable."""
    html = await _get(f"{BASE_URL}/", referer=None)
    return bool(html and "hentaifox" in html.lower())
