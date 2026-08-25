"""
cron_orchestrator.py — v12.41: single supervisor for the background crons.

Previously worker.py spawned prefetch_cron / dedup_cron /
details_prefetch_cron as three independent asyncio tasks. A silent crash
inside one was invisible until someone noticed its dashboard stop moving,
and each task kept its own Turso/Mongo connections warm forever.

This module owns ONE task that supervises the three child tasks:
  * spawns each cron's existing run_forever() unchanged,
  * watches them; if a child dies unexpectedly it logs LOUDLY and
    respawns it after a backoff,
  * exposes a combined status() for a future /crons admin surface.

Each cron still owns its own cadence, env toggles and fail-open contract —
this is supervision, not a rewrite. rescrape.py is a library (no
run_forever) so it is NOT supervised here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("miniapp.cron_orchestrator")

_RESPAWN_BACKOFF_SEC = 30
_state: Dict[str, Any] = {}


def status() -> Dict[str, Any]:
    return dict(_state)


async def _supervise(name: str, factory: Callable[[], Any]) -> None:
    """Run factory() forever; respawn on unexpected exit with backoff."""
    crashes = 0
    while True:
        _state[name] = {"state": "running", "crashes": crashes,
                        "last_change": int(time.time())}
        try:
            await factory()
            # run_forever() returning normally means the cron chose to stop
            # (disabled) — treat as clean exit, do NOT respawn hot.
            _state[name] = {"state": "exited-clean", "crashes": crashes,
                            "last_change": int(time.time())}
            log.warning("cron_orchestrator: %s exited cleanly — respawning in %ds",
                        name, _RESPAWN_BACKOFF_SEC)
        except asyncio.CancelledError:
            _state[name] = {"state": "cancelled", "crashes": crashes,
                            "last_change": int(time.time())}
            log.info("cron_orchestrator: %s cancelled — not respawning", name)
            raise
        except Exception as e:  # noqa: BLE001
            crashes += 1
            _state[name] = {"state": "crashed", "crashes": crashes,
                            "error": str(e)[:200],
                            "last_change": int(time.time())}
            log.exception("cron_orchestrator: %s crashed (%s) — respawning in %ds",
                          name, e, _RESPAWN_BACKOFF_SEC)
        try:
            await asyncio.sleep(_RESPAWN_BACKOFF_SEC)
        except asyncio.CancelledError:
            raise


async def run_forever(prefetch_cron, dedup_cron, details_cron) -> None:
    """Supervise the three crons as child tasks. Never raises."""
    children = []
    if prefetch_cron is not None:
        children.append(asyncio.create_task(
            _supervise("prefetch_cron", prefetch_cron.run_forever),
            name="sup:prefetch_cron"))
    if dedup_cron is not None:
        children.append(asyncio.create_task(
            _supervise("dedup_cron", dedup_cron.run_forever),
            name="sup:dedup_cron"))
    if details_cron is not None:
        children.append(asyncio.create_task(
            _supervise("details_prefetch_cron", details_cron.run_forever),
            name="sup:details_prefetch_cron"))
    if not children:
        log.warning("cron_orchestrator: no crons to supervise — idling")
        return
    try:
        await asyncio.gather(*children)
    except asyncio.CancelledError:
        for c in children:
            c.cancel()
        await asyncio.gather(*children, return_exceptions=True)
        raise
