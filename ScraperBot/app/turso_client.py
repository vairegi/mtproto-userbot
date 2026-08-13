"""
turso_client.py — libsql (Turso) client for the SHARED cache DB.

Writes into the SAME table BOT 0 reads from. Table shape is the standard
BOT 0 uses:

    CREATE TABLE IF NOT EXISTS nhentai_cache (
        key         TEXT PRIMARY KEY,
        payload     TEXT NOT NULL,       -- JSON
        updated_at  INTEGER NOT NULL,    -- unix seconds
        expires_at  INTEGER NOT NULL     -- unix seconds
    )

BOT 1 never migrates or drops this table; it only CREATE-IF-NOT-EXISTS on
boot so a fresh Turso DB is bootstrappable without BOT 0 running first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Optional

from .config import settings

log = logging.getLogger("scraperbot.turso")

try:
    import libsql_client  # type: ignore
    HAVE_LIBSQL = True
except Exception as e:  # noqa: BLE001
    libsql_client = None  # type: ignore
    HAVE_LIBSQL = False
    log.warning("libsql-client not importable — Turso disabled (%s)", e)


_client_lock = threading.Lock()
_client = None  # AsyncClient
_client_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_client():
    """Return a lazily-created libsql AsyncClient bound to a dedicated loop.

    We keep one persistent client for the whole process. Requests are
    driven from FastAPI's async workers directly (no per-thread loops
    needed — FastAPI already provides one).
    """
    global _client
    if not HAVE_LIBSQL:
        return None
    if not settings.turso_url or not settings.turso_token:
        return None
    with _client_lock:
        if _client is None:
            try:
                _client = libsql_client.create_client(
                    url=settings.turso_url,
                    auth_token=settings.turso_token,
                )
                log.info("Turso client initialised")
            except Exception as e:  # noqa: BLE001
                log.error("Turso init failed: %s", e)
                _client = None
        return _client


def turso_available() -> bool:
    return _get_client() is not None


async def bootstrap_schema() -> None:
    """CREATE TABLE IF NOT EXISTS. Idempotent; safe to call every boot."""
    c = _get_client()
    if c is None:
        return
    try:
        await c.execute("""
            CREATE TABLE IF NOT EXISTS nhentai_cache (
                key         TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                updated_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL
            )
        """)
        log.info("Turso: schema OK")
    except Exception as e:  # noqa: BLE001
        log.warning("Turso bootstrap failed: %s", e)


async def get(key: str) -> Optional[dict]:
    c = _get_client()
    if c is None:
        return None
    try:
        rs = await c.execute(
            "SELECT payload, updated_at, expires_at FROM nhentai_cache WHERE key = ?",
            [key],
        )
        rows = list(rs.rows) if rs and getattr(rs, "rows", None) else []
        if not rows:
            return None
        r = rows[0]
        try:
            payload = json.loads(r[0])
        except Exception:
            return None
        return {
            "payload": payload,
            "updated_at": int(r[1] or 0),
            "expires_at": int(r[2] or 0),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("Turso get(%s) failed: %s", key, e)
        return None


async def put(key: str, payload: Any, ttl_sec: int) -> bool:
    c = _get_client()
    if c is None:
        return False
    try:
        now = int(time.time())
        body = json.dumps(payload, ensure_ascii=False)
        await c.execute(
            "INSERT INTO nhentai_cache (key, payload, updated_at, expires_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "  payload    = excluded.payload, "
            "  updated_at = excluded.updated_at, "
            "  expires_at = excluded.expires_at",
            [key, body, now, now + int(ttl_sec)],
        )
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("Turso put(%s) failed: %s", key, e)
        return False


async def close() -> None:
    global _client
    with _client_lock:
        c = _client
        _client = None
    if c is not None:
        try:
            await c.close()
        except Exception:  # noqa: BLE001
            pass
