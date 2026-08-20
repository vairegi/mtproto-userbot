"""admin.py — /trigger /pause /resume — admin-key gated."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from .. import mongo_client
from ..auth import require_admin
from ..services import list_sweeper, details_sweeper

router = APIRouter()


@router.post("/pause")
def pause(_=Depends(require_admin)) -> dict:
    mongo_client.set_paused(True)
    return {"ok": True, "paused": True}


@router.post("/resume")
def resume(_=Depends(require_admin)) -> dict:
    mongo_client.set_paused(False)
    return {"ok": True, "paused": False}


@router.post("/trigger")
async def trigger(what: str = "list", _=Depends(require_admin)) -> dict:
    """Kick a single sweep now. `what` is one of: list, details, both."""
    what = (what or "list").strip().lower()
    tasks = []
    if what in ("list", "both"):
        tasks.append(asyncio.create_task(list_sweeper.sweep_once()))
    if what in ("details", "both"):
        tasks.append(asyncio.create_task(details_sweeper.sweep_once()))
    if not tasks:
        return {"ok": False, "error": "what must be one of: list, details, both"}
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append({"error": str(r)})
        else:
            out.append(r)
    return {"ok": True, "what": what, "results": out}
