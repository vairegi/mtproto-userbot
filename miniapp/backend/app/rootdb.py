"""
rootdb.py — v12.53: deterministic loader for the REPO-ROOT db.py.

Why this exists
---------------
The repo contains TWO db.py files:
  * <repo>/db.py                  — the bot DB (connect(), galleries,
                                    get_cached_gallery_ids(), is_admin_user, ...)
  * miniapp/backend/app/db.py     — miniapp-local package module, ONLY valid
                                    when imported as `app.db`.

Legacy miniapp modules used a bare `import db`, expecting the repo-root one.
If miniapp/backend/app is on sys.path, `import db` can resolve to app/db.py
instead, which crashes on its relative `from .config import settings` import
and kills every route module that touched it at import time.

This loader bypasses sys.path entirely: it locates <repo>/db.py by absolute
file path and executes it under a private module name — deterministic
regardless of path order or import context.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys

log = logging.getLogger("miniapp.rootdb")

_PRIVATE_NAME = "_repo_root_db"


def _repo_root() -> str:
    d = os.path.dirname(os.path.abspath(__file__))  # .../app
    for _ in range(3):                              # backend -> miniapp -> repo root
        d = os.path.dirname(d)
    return d


def load():
    """Load and return the repo-root db.py module. Never raises; returns None
    on failure so callers can degrade gracefully instead of killing imports."""
    if _PRIVATE_NAME in sys.modules:
        return sys.modules[_PRIVATE_NAME]
    root = _repo_root()
    path = os.path.join(root, "db.py")
    if not os.path.isfile(path):
        log.warning("rootdb: repo-root db.py not found at %s — bot-db features disabled", path)
        return None
    try:
        if root not in sys.path:
            sys.path.append(root)
        spec = importlib.util.spec_from_file_location(_PRIVATE_NAME, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_PRIVATE_NAME] = mod  # cache BEFORE exec (re-entry safe)
        spec.loader.exec_module(mod)
        log.info("rootdb: loaded repo-root db.py from %s", path)
        return mod
    except Exception as e:  # noqa: BLE001
        sys.modules.pop(_PRIVATE_NAME, None)
        log.warning("rootdb: failed to load %s: %s", path, e)
        return None
