"""
layout.py — PUBLIC read endpoint for the card-grid layout knobs
(v12.10 #6+#7, v12.11 #4)

The mini-app frontend needs the grid layout (cards per row, gap,
horizontal page padding) at boot BEFORE any admin auth dance, so this
read is unauthenticated. The values are layout cosmetics only —
nothing sensitive. The WRITE side lives in routes/admin.py behind
require_admin.

Endpoints:
  GET /api/layout  → { cards_per_row: int, card_gap: float, app_pad_x: int }
"""
from __future__ import annotations

from fastapi import APIRouter

from .. import db

router = APIRouter(prefix="/api/layout", tags=["layout"])


@router.get("")
def get_layout() -> dict:
    return {
        "cards_per_row": db.get_setting("layout_cards_per_row", 2),
        "card_gap":      db.get_setting("layout_card_gap", 0),
        # v12.11 (#4): horizontal page padding (0/4/8/12/16/20/24 px).
        "app_pad_x":     db.get_setting("layout_app_pad_x", 0),
    }
