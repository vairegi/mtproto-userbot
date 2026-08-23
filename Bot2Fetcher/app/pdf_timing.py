"""
pdf_timing.py — adaptive wait for @Gallery_DLBot's PDF reply.
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
