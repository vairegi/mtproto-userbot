"""
turso_client.py — v12.4 Turso (libSQL) connection + schema helper.

Purpose
-------
Provide a single, lazily-initialised libSQL client for the whole process,
plus a `_ensure_schema()` idempotent DDL bootstrapper that creates the two
tables the mini-app cache layer needs:

    nhentai_cache        long-TTL blob cache (gallery detail, search pages,
                         suggestions, trending)
    nhentai_ratelimit    shared per-endpoint token bucket, sized to the
                         anon quotas documented at
                         nhentai.net/api/v2/openapi.json

Environment
-----------
    TURSO_DATABASE_URL   libsql://<db>-<user>.turso.io   (required)
    TURSO_AUTH_TOKEN     long JWT from the Turso dashboard (required)

If either is missing OR the libsql_client package is not installed, the
`turso_available()` helper returns False. Every caller MUST check this
and fall back to the Mongo path — Turso is an accelerator, not a hard
dependency.

Threading model
---------------
libsql-client is async by design. The mini-app is sync-friendly (FastAPI
def handlers run in a threadpool), so we expose SYNC helpers that create
a fresh event loop per call and dispose it. This is intentionally simple
and matches the pattern already in scraper_bridge.py (_run_async).

Failure semantics
-----------------
Every public function catches every exception and:
  * returns None / [] / False for reads
  * returns False for writes
The mini-app UI must NEVER go down because Turso is unreachable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as _time
from typing import Any, Optional

log = logging.getLogger("miniapp.turso")

# ---------------------------------------------------------------------------
# Env + client acquisition (lazy)
# ---------------------------------------------------------------------------
_TURSO_URL   = os.environ.get("TURSO_DATABASE_URL", "").strip()
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN",   "").strip()

_schema_ready = False
_schema_lock = threading.Lock()

try:
    import libsql_client   # type: ignore
    _HAVE_LIBSQL = True
except Exception:  # noqa: BLE001
    libsql_client = None    # type: ignore
    _HAVE_LIBSQL = False


def turso_available() -> bool:
    """Return True iff the env is configured AND libsql_client is importable.
    Callers MUST branch on this — the module never raises on missing config.
    """
    return bool(_HAVE_LIBSQL and _TURSO_URL and _TURSO_TOKEN)


def _run(coro):
    """Run a libsql coroutine on a fresh event loop and dispose it.

    libsql_client.Client is bound to whatever loop first used it. Reusing
    a persistent loop across a threadpool leaks connections; creating a
    fresh loop per call is cheap (µs) and matches how FastAPI's sync
    handlers already work.
    """
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(coro)
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(None)


def _make_client():
    """Build a fresh libsql_client for one call. Cheap — it's HTTP under
    the hood, no long-lived TCP socket. Returning a NEW client per call
    also side-steps the 'client bound to closed loop' failure mode."""
    if not turso_available():
        return None
    try:
        return libsql_client.create_client(url=_TURSO_URL, auth_token=_TURSO_TOKEN)
    except Exception as e:  # noqa: BLE001
        log.warning("turso: create_client failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Schema (idempotent)
# ---------------------------------------------------------------------------
_DDL_STATEMENTS = [
    # Cache blob table. `payload` is stored as JSON text (SQLite has no
    # native JSON blob type; TEXT is efficient enough for our sizes).
    # `expires_at` is unix-epoch seconds; a separate index lets a periodic
    # sweep purge stale rows (Turso has no built-in TTL indexes yet).
    """CREATE TABLE IF NOT EXISTS nhentai_cache (
        key         TEXT    PRIMARY KEY,
        payload     TEXT    NOT NULL,
        cached_at   INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        ttl_sec     INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_nhcache_expires ON nhentai_cache(expires_at)",

    # Shared per-endpoint token bucket. Row per bucket_id.
    # tokens/updated_at drift with time; the try_consume() SQL performs
    # the refill + spend atomically via a single UPDATE.
    """CREATE TABLE IF NOT EXISTS nhentai_ratelimit (
        bucket_id     TEXT    PRIMARY KEY,
        tokens        REAL    NOT NULL,
        capacity      INTEGER NOT NULL,
        rate_per_sec  REAL    NOT NULL,
        updated_at    REAL    NOT NULL
    )""",
]


async def _ensure_schema_async() -> bool:
    client = _make_client()
    if client is None:
        return False
    try:
        for stmt in _DDL_STATEMENTS:
            await client.execute(stmt)
        return True
    finally:
        await client.close()


def ensure_schema() -> bool:
    """Create the two Turso tables + indexes if they don't exist.
    Safe to call repeatedly; the first successful call short-circuits."""
    global _schema_ready
    if _schema_ready:
        return True
    if not turso_available():
        return False
    with _schema_lock:
        if _schema_ready:
            return True
        try:
            ok = _run(_ensure_schema_async())
        except Exception as e:  # noqa: BLE001
            log.warning("turso: ensure_schema failed: %s", e)
            return False
        _schema_ready = bool(ok)
        if _schema_ready:
            log.info("turso: schema ready (nhentai_cache + nhentai_ratelimit)")
        return _schema_ready


# ---------------------------------------------------------------------------
# Health + diagnostics
# ---------------------------------------------------------------------------
async def _health_async() -> dict:
    client = _make_client()
    if client is None:
        return {"available": False, "reason": "no client"}
    try:
        t0 = _time.time()
        r = await client.execute("SELECT 1")
        dur_ms = int((_time.time() - t0) * 1000)
        ok = bool(r.rows and r.rows[0][0] == 1)
        return {"available": ok, "latency_ms": dur_ms}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e)[:120]}
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


def health() -> dict:
    """Diagnostic: is Turso reachable, and how fast? Used by /diag."""
    if not turso_available():
        return {"available": False, "reason": "TURSO_DATABASE_URL/TURSO_AUTH_TOKEN not set"}
    try:
        return _run(_health_async())
    except Exception as e:  # noqa: BLE001
        return {"available": False, "reason": str(e)[:120]}


# ---------------------------------------------------------------------------
# Low-level query helpers exposed for the cache module. Everything else
# (get/put/try_consume) lives in nhentai_cache.py — this module only
# provides the raw executor so it's easy to swap engines later.
# ---------------------------------------------------------------------------
async def _execute_async(sql: str, args: Optional[list] = None):
    client = _make_client()
    if client is None:
        return None
    try:
        if args is None:
            return await client.execute(sql)
        return await client.execute(sql, args)
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


def execute(sql: str, args: Optional[list] = None):
    """Run one SQL statement synchronously. Returns the libsql ResultSet
    (has .rows attribute) or None on any error.
    Caller MUST tolerate None and fall back to the Mongo path.
    """
    if not turso_available():
        return None
    if not _schema_ready:
        ensure_schema()
    try:
        return _run(_execute_async(sql, args))
    except Exception as e:  # noqa: BLE001
        log.warning("turso: execute failed (%s): %s", sql.split(None, 1)[0], e)
        return None
