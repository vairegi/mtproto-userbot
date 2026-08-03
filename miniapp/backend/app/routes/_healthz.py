"""
_healthz.py — Underscore-prefixed so mount_all() skips it.

This file exists purely as an example of how to write a route module that
you want to KEEP in the folder for reference but NOT auto-mount. The
autoloader in routes/__init__.py skips any file whose name starts with '_'.

If you want to enable it later, rename to healthz.py.
"""
from fastapi import APIRouter

router = APIRouter(prefix="/api/_healthz", tags=["health"])


@router.get("")
def healthz() -> dict:
    return {"ok": True}
