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
import concurrent.futures
import logging
import os
import threading
import time as _time
from typing import Any, Optional

log = logging.getLogger("miniapp.turso")

# v12.5: dedicated single-thread executor used only when the calling
# thread already has a running asyncio loop (e.g. inside the worker's
# prefetch_cron sweep). A single reusable worker thread avoids the
# per-call thread-spawn overhead while still giving each libsql
# coroutine its own fresh event loop. Lazy-init so import-time does no
# work in short-lived tools (compileall, tests_v2_smoke).
_bridge_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
_bridge_lock = threading.Lock()


def _get_bridge_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _bridge_executor
    if _bridge_executor is not None:
        return _bridge_executor
    with _bridge_lock:
        if _bridge_executor is None:
            _bridge_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="turso-bridge",
            )
        return _bridge_executor

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


def _run_in_fresh_loop(coro):
    """Execute ``coro`` on a brand-new event loop bound to *this* thread.

    ONLY safe to call from a thread with no running loop. Kept as a
    private helper so both the sync-fast-path (``_run``) and the
    thread-bridge (``_run_via_bridge``) can share exactly the same
    close/cleanup semantics.
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


def _run_via_bridge(coro_factory):
    """Run a libsql coroutine on a background thread with its own loop.

    The caller passes a **coroutine factory** (a zero-arg callable that
    RETURNS the coroutine) rather than a coroutine object, because a
    coroutine object cannot cross threads safely — it's bound to whatever
    loop first awaited it. Building the coroutine INSIDE the bridge
    thread guarantees the libsql client + its awaited call live entirely
    on the bridge thread's fresh loop.

    Returns the coroutine's result, or raises whatever the coroutine
    raised (caller’s try/except is what turns those into log warnings).
    """
    ex = _get_bridge_executor()
    fut = ex.submit(_run_in_fresh_loop, coro_factory())
    return fut.result()


def _run(coro_factory):
    """Public dispatcher. Accepts a zero-arg **coroutine factory** and:

    * If the calling thread has NO running asyncio loop (FastAPI sync
      handlers, tests, compileall workers) — run the coroutine inline on
      a fresh loop in this thread. Same as v12.4 behaviour, zero cost.

    * If the calling thread has a RUNNING loop (e.g. prefetch_cron
      running inside the worker's asyncio loop) — hand the factory to
      the ``turso-bridge`` worker thread, which owns its own fresh loop
      and can call ``loop.run_until_complete`` legally.

    Historic callers passed a coroutine object directly. To keep that
    working, we detect a coroutine here and wrap it in a trivial factory
    — BUT only the sync fast-path can safely consume that pre-built
    coroutine (a coroutine bound to a different thread's loop cannot be
    driven from the bridge thread). If we're on the bridge branch and
    the caller passed a plain coroutine, we raise a clear error rather
    than silently deadlock.
    """
    is_factory = callable(coro_factory) and not asyncio.iscoroutine(coro_factory)

    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None

    if running is None:
        # Fast path — same behaviour as v12.4. Works for both a factory
        # and a pre-built coroutine.
        coro = coro_factory() if is_factory else coro_factory
        return _run_in_fresh_loop(coro)

    # Bridge path — we're inside somebody else's running loop.
    if not is_factory:
        raise RuntimeError(
            "turso._run: cannot marshal a pre-built coroutine across threads. "
            "Pass a zero-arg factory (lambda: my_async(...)) when called from an "
            "async context."
        )
    return _run_via_bridge(coro_factory)


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
            ok = _run(_ensure_schema_async)   # pass FACTORY, not coroutine
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
        return _run(_health_async)   # pass FACTORY, not coroutine
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
    # Factory closure captures sql+args so the coroutine is built inside
    # the bridge thread when we're on the bridge branch. v12.5 fix for
    # "Cannot run the event loop while another loop is running".
    def _factory():
        return _execute_async(sql, args)
    try:
        return _run(_factory)
    except Exception as e:  # noqa: BLE001
        log.warning("turso: execute failed (%s): %s", sql.split(None, 1)[0], e)
        return None
