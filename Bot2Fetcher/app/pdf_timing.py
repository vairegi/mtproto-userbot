"""
pdf_timing.py — adaptive wait for @Gallery_DLBot's PDF reply.

Lesson from BOT 0 v11.3: @Gallery_DLBot needs ~1.6 s per page to build the
PDF; a flat 60/480 s cap timed out 200-page galleries. Same formula here.
"""
from __future__ import annotations

BASE_S = 90.0
PER_PAGE_S = 1.7
MIN_S = 180.0
MAX_S = 900.0


def compute_pdf_timeout(pages: int) -> float:
    try:
        p = int(pages or 0)
    except (TypeError, ValueError):
        p = 0
    t = BASE_S + p * PER_PAGE_S
    return max(MIN_S, min(MAX_S, t))
