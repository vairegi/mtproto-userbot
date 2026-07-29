"""
hf_scraper.py — Direct scraper for hentaifox.com, used by:
  - /search <keyword>  (Admin Bot inline results)
  - relay.py fallback metadata (when SOURCE_API_BASE/KEY are not configured
    and Bot 1's cover post is not detected in time)

No external API/service required — this talks to hentaifox.com directly with
a plain `requests` GET (BeautifulSoup for parsing), matching how a normal
browser would load the page. Defensive parsing throughout: if hentaifox
changes its markup, functions return an empty/None result rather than raising,
so callers can show "search unavailable" instead of crashing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

from logging_setup import setup_logging

log = setup_logging("hf_scraper")

BASE_URL = "https://hentaifox.com"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}
_TIMEOUT = 15.0


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


async def _get(url: str, params: Optional[dict] = None) -> Optional[str]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, headers=_HEADERS, follow_redirects=True) as c:
            r = await c.get(url, params=params)
            if r.status_code != 200:
                log.warning("hf_scraper HTTP %s for %s", r.status_code, url)
                if r.status_code == 404:
                    return None
                return None
            return r.text
    except Exception as e:  # noqa: BLE001
        log.warning("hf_scraper request failed: %s", e)
        return None


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
    html = await _get(f"{BASE_URL}/search/", params=params)
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
    html = await _get(url)
    if html is None:
        return None
    try:
        return _parse_gallery_html(html, gallery_id=gid)
    except Exception as e:  # noqa: BLE001
        log.warning("hf_scraper: failed to parse gallery page: %s", e)
        return None
