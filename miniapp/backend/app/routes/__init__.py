"""
routes/__init__.py — Auto-mount every route file in this package.

To add a new endpoint file:
  1. Drop foo.py in this folder.
  2. Inside it declare  `router = APIRouter(prefix="/api/foo", tags=["foo"])`.
  3. Done.  main.py calls mount_all(app) which imports every *.py here and
     mounts its `router` attribute.

Files starting with `_` are skipped.
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from pathlib import Path

from fastapi import APIRouter, FastAPI

log = logging.getLogger("miniapp.routes")

_HERE = Path(__file__).resolve().parent


def mount_all(app: FastAPI) -> None:
    for m in pkgutil.iter_modules([str(_HERE)]):
        if m.name.startswith("_"):
            continue
        try:
            mod = importlib.import_module(f"app.routes.{m.name}")
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to import route module %s: %s", m.name, e)
            continue
        router = getattr(mod, "router", None)
        if isinstance(router, APIRouter):
            app.include_router(router)
            log.info("Mounted route module: %s", m.name)
        else:
            log.warning("Module %s has no `router` — skipping", m.name)
