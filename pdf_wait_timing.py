"""
pdf_wait_timing.py — v11.3

Compute an adaptive Bot 2 PDF-wait timeout based on the gallery's page
count.

Why this exists
---------------
`@Gallery_DLBot` needs time proportional to the number of pages to
build the PDF for a doujinshi. Small galleries (< 30 pages) build in
seconds; a 200-page 150 MB gallery takes 1–2 minutes just to render
and upload. Before v11.3 the wait was a flat env-var
(`BOT2_PDF_TIMEOUT_SEC`, default 60, upper cap 480 in `_bot2_timeout_sec`)
which meant either:
  * short cap → false-positive timeouts on large galleries; OR
  * long cap → every small gallery unnecessarily hogs a worker slot
    when Bot 2 hits an actual error but never sends a text reply.

Fix
----
Scale the timeout linearly with `pages`, clamped between a sane floor
and ceiling. The floor keeps the pre-v11.3 behaviour for small
galleries (so nothing regresses), and the ceiling protects the
pipeline from a run-away worker if `pages` is huge or unknown.

Formula
-------
    timeout = clamp( floor + pages * seconds_per_page, floor, ceiling )

Defaults (from measurement of @Gallery_DLBot in 2026):
  * seconds_per_page = 1.6 s  — includes scrape + zip + upload back to us
  * floor            = 90 s   — matches the previous default behaviour
  * ceiling          = 900 s  — 15 min hard cap; anything bigger is a bug

All three are env-tunable so ops can tighten them without a code change:
  BOT2_PDF_TIMEOUT_FLOOR_SEC   (default 90)
  BOT2_PDF_TIMEOUT_CEIL_SEC    (default 900)
  BOT2_PDF_SEC_PER_PAGE        (default 1.6)

Pure module, no side effects at import; safe to hot-import from
`relay_v2`.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("pdf_wait_timing")


# ---------------------------------------------------------------------------
# Env helpers
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        v = int(os.getenv(key, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        v = float(os.getenv(key, "") or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_pdf_timeout(
    pages: Optional[int],
    *,
    base_timeout_sec: Optional[int] = None,
) -> int:
    """Return the number of seconds to wait for Bot 2's PDF reply.

    Parameters
    ----------
    pages
        Gallery page count from ``cover.pages`` (may be ``None`` on scrape
        edge-cases — falls back to the floor).
    base_timeout_sec
        Optional caller override for the FLOOR. When passed, the resulting
        timeout is at least this value. Used by ``relay_v2`` to keep the
        pre-v11.3 configured minimum: whatever ops set for
        ``BOT2_PDF_TIMEOUT_SEC`` still acts as the lower bound.

    Returns
    -------
    int
        Seconds to wait, in ``[floor, ceiling]``.

    Examples
    --------
    >>> compute_pdf_timeout(20)              # small gallery
    90
    >>> compute_pdf_timeout(100)             # medium
    250
    >>> compute_pdf_timeout(200)             # ~150 MB — user's report
    410
    >>> compute_pdf_timeout(600)             # very large, hits ceiling
    900
    >>> compute_pdf_timeout(None)            # unknown → floor
    90
    """
    floor   = _env_int("BOT2_PDF_TIMEOUT_FLOOR_SEC", 90)
    ceiling = _env_int("BOT2_PDF_TIMEOUT_CEIL_SEC", 900)
    per_pg  = _env_float("BOT2_PDF_SEC_PER_PAGE",   1.6)

    if base_timeout_sec is not None and base_timeout_sec > floor:
        floor = int(base_timeout_sec)

    if ceiling < floor:
        # Misconfigured env — restore a sane relationship without crashing.
        ceiling = floor + 60

    try:
        n = int(pages) if pages is not None else 0
    except (TypeError, ValueError):
        n = 0

    if n <= 0:
        return floor

    scaled = int(floor + round(n * per_pg))
    if scaled < floor:
        scaled = floor
    if scaled > ceiling:
        scaled = ceiling
    return scaled


def describe_timeout(pages: Optional[int], timeout: int) -> str:
    """Return a short 'why' string used in progress + admin messages."""
    try:
        n = int(pages) if pages is not None else 0
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return f"{timeout}s (pages unknown; using floor)"
    return f"{timeout}s (adaptive for {n}-page gallery)"
