"""
url_utils.py — URL validation, normalisation, hashing, and slug extraction.

Used by:
- Admin Bot /fetch to validate + hash + dedupe inside a batch.
- Relay matcher (§7b) to compare Bot 1's post text against a slug/ID
  derivable from the URL.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

# Very permissive: any http(s) URL with a hostname and a /gallery/ID-style path.
# Different gallery sites have slightly different URL shapes; we do a soft
# structural check + a hard scheme/host check. Bot 1 itself is the ultimate
# validator (it will reject bad URLs). We just protect against obvious junk
# from a typo'd /fetch command.
_URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedGalleryURL:
    original: str
    normalised: str
    url_hash: str
    slug_candidates: Tuple[str, ...]   # substrings to look for in Bot 1's text


def _normalise(url: str) -> str:
    """Lowercase host, strip fragment, strip trailing slash, drop tracking params."""
    u = urlparse(url.strip())
    if not u.scheme or not u.netloc:
        return url.strip()
    host = u.netloc.lower()
    path = u.path or "/"
    # collapse duplicate slashes
    path = re.sub(r"/{2,}", "/", path)
    if path.endswith("/") and len(path) > 1:
        path = path.rstrip("/")
    # Drop query entirely for hashing purposes — gallery IDs are in the path
    # for hentaifox-style URLs. If a site relies on ?id= that's still captured
    # via slug_candidates below.
    return urlunparse((u.scheme.lower(), host, path, "", "", ""))


def _slug_candidates(url: str) -> Tuple[str, ...]:
    """Extract likely identifiers a Bot 1 post might mention:
       - trailing numeric ID from the path (e.g. /gallery/123456)
       - trailing slug segment (last non-empty path component)
       - any ?id= / ?g= query value
    """
    u = urlparse(url.strip())
    out: List[str] = []
    parts = [p for p in (u.path or "").split("/") if p]
    if parts:
        last = parts[-1]
        out.append(last)
        m = re.search(r"(\d{3,})", last)
        if m:
            out.append(m.group(1))
    # Query values
    if u.query:
        for kv in u.query.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                if k.lower() in {"id", "g", "gallery", "gid"} and v:
                    out.append(v)
    # de-dupe, preserve order
    seen: set = set()
    uniq: List[str] = []
    for s in out:
        s = s.strip()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return tuple(uniq)


def hash_url(normalised: str) -> str:
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def validate_and_parse(url: str) -> Optional[ParsedGalleryURL]:
    url = (url or "").strip()
    if not url or not _URL_RE.match(url):
        return None
    normalised = _normalise(url)
    if not normalised.startswith(("http://", "https://")):
        return None
    return ParsedGalleryURL(
        original=url,
        normalised=normalised,
        url_hash=hash_url(normalised),
        slug_candidates=_slug_candidates(url),
    )


@dataclass
class BatchParseResult:
    accepted: List[ParsedGalleryURL]
    rejected: List[Tuple[str, str]]      # (raw_line, reason)
    duplicates_in_batch: List[str]       # normalised URLs

    def summary(self) -> str:
        return (
            f"{len(self.accepted)} queued, "
            f"{len(self.rejected)} rejected, "
            f"{len(self.duplicates_in_batch)} skipped as duplicates"
        )


def parse_batch(text: str, max_links: int) -> BatchParseResult:
    """Parse the body of a /fetch command. One URL per line."""
    accepted: List[ParsedGalleryURL] = []
    rejected: List[Tuple[str, str]] = []
    dupes: List[str] = []
    seen_hashes: set = set()

    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    for line in lines:
        # Skip lines that are clearly not URLs (e.g. the "/fetch" prefix itself)
        if line.lower().startswith("/fetch"):
            continue
        parsed = validate_and_parse(line)
        if parsed is None:
            rejected.append((line, "not a valid http(s) URL"))
            continue
        if parsed.url_hash in seen_hashes:
            dupes.append(parsed.normalised)
            continue
        if len(accepted) >= max_links:
            rejected.append((line, f"batch cap reached ({max_links})"))
            continue
        seen_hashes.add(parsed.url_hash)
        accepted.append(parsed)

    return BatchParseResult(
        accepted=accepted, rejected=rejected, duplicates_in_batch=dupes
    )
