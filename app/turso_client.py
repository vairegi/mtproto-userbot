"""
turso_client.py — Turso client using the raw HTTP API (v2/pipeline).

v1.5 rewrite: schema-proof INSERT.

BOT 0's real Turso schema (confirmed via `SELECT sql FROM sqlite_schema`):

    CREATE TABLE nhentai_cache (
        "key"       TEXT PRIMARY KEY,
        payload     TEXT NOT NULL,
        cached_at   INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        ttl_sec     INTEGER NOT NULL,
        updated_at  INTEGER NOT NULL DEFAULT 0
    )

Earlier ScraperBot versions guessed the columns and hit NOT NULL / no-such-
column errors as each new field revealed itself. v1.5 stops guessing:

  1. On bootstrap: read `PRAGMA table_info(nhentai_cache)` and cache the
     real column set + which are NOT NULL.
  2. On INSERT: build the column list from the discovered schema. Every
     column present in the table is written; missing-in-code columns get
     a safe default (current epoch for *_at, TTL for ttl_sec).
  3. If the table doesn't exist yet, CREATE it with BOT 0's exact schema.

Result: future schema drift no longer breaks writes.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import settings

log = logging.getLogger("scraperbot.turso")


def _http_base_url() -> str:
    url = (settings.turso_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    return url


_client_lock = threading.Lock()
_client: Optional[httpx.AsyncClient] = None

# Discovered schema — populated by bootstrap_schema().
#   _COLS = {"key", "payload", "cached_at", ...}
#   _NOT_NULL = {"key", "payload", "cached_at", ...}
_COLS: set[str] = set()
_NOT_NULL: set[str] = set()
_SCHEMA_READY = False


def _get_client() -> Optional[httpx.AsyncClient]:
    global _client
    base = _http_base_url()
    if not base or not settings.turso_token:
        return None
    with _client_lock:
        if _client is None:
            try:
                _client = httpx.AsyncClient(
                    base_url=base,
                    timeout=20.0,
                    headers={
                        "Authorization": f"Bearer {settings.turso_token}",
                        "Content-Type": "application/json",
                    },
                )
                log.info("Turso HTTP client initialised (base=%s)", base)
            except Exception as e:  # noqa: BLE001
                log.error("Turso init failed: %s", e)
                _client = None
        return _client


def turso_available() -> bool:
    return bool(_http_base_url()) and bool(settings.turso_token)


async def _pipeline(stmts: List[dict]) -> Optional[List[dict]]:
    c = _get_client()
    if c is None:
        return None
    body = {
        "requests": [{"type": "execute", "stmt": s} for s in stmts]
                    + [{"type": "close"}]
    }
    try:
        r = await c.post("/v2/pipeline", json=body)
    except httpx.HTTPError as e:
        log.warning("Turso pipeline transport error: %s", e)
        return None
    if r.status_code != 200:
        log.warning("Turso pipeline HTTP %s: %s", r.status_code, r.text[:200])
        return None
    try:
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("Turso pipeline non-JSON: %s", e)
        return None

    out: List[dict] = []
    for entry in (data.get("results") or []):
        if entry.get("type") == "error":
            log.warning("Turso pipeline stmt error: %s",
                        (entry.get("error") or {}).get("message"))
            out.append({})
            continue
        resp = entry.get("response") or {}
        if resp.get("type") == "execute":
            out.append(resp.get("result") or {})
    return out


def _cell_value(cell: Any) -> Any:
    if isinstance(cell, dict):
        t = cell.get("type")
        v = cell.get("value")
        if t == "null" or v is None:
            return None
        if t == "integer":
            try: return int(v)
            except (TypeError, ValueError): return v
        if t == "float":
            try: return float(v)
            except (TypeError, ValueError): return v
        return v
    return cell


def _row_named(cols: list, row: list) -> dict:
    out: dict = {}
    for i, c in enumerate(cols or []):
        name = c.get("name") if isinstance(c, dict) else str(c)
        if not name:
            name = f"col_{i}"
        val = row[i] if i < len(row) else None
        out[name] = _cell_value(val)
    return out


async def _discover_columns() -> None:
    """PRAGMA table_info(nhentai_cache) → populate _COLS / _NOT_NULL."""
    global _COLS, _NOT_NULL, _SCHEMA_READY
    res = await _pipeline([{"sql": "PRAGMA table_info(nhentai_cache)"}])
    if not res:
        return
    result = res[0] or {}
    cols_meta = result.get("cols") or []
    rows = result.get("rows") or []
    _COLS = set()
    _NOT_NULL = set()
    for row in rows:
        named = _row_named(cols_meta, row)
        name = str(named.get("name") or "").strip()
        if not name:
            continue
        _COLS.add(name)
        # SQLite PRAGMA returns notnull=1 for NOT NULL columns
        if int(named.get("notnull") or 0) == 1:
            _NOT_NULL.add(name)
    _SCHEMA_READY = True
    log.info("Turso schema discovered: cols=%s not_null=%s",
             sorted(_COLS), sorted(_NOT_NULL))


async def bootstrap_schema() -> None:
    """CREATE TABLE IF NOT EXISTS (using BOT 0's exact shape), then
    read PRAGMA table_info to lock in the real column set."""
    if not turso_available():
        log.info("Turso not configured — skipping schema bootstrap")
        return
    # BOT 0's exact schema — CREATE IF NOT EXISTS is a no-op if BOT 0 got
    # here first, and correctly bootstraps a fresh DB if BOT 1 is first.
    create_sql = (
        'CREATE TABLE IF NOT EXISTS nhentai_cache ('
        '"key" TEXT PRIMARY KEY, '
        'payload TEXT NOT NULL, '
        'cached_at INTEGER NOT NULL, '
        'expires_at INTEGER NOT NULL, '
        'ttl_sec INTEGER NOT NULL, '
        'updated_at INTEGER NOT NULL DEFAULT 0'
        ')'
    )
    res = await _pipeline([{"sql": create_sql}])
    if res is None:
        log.warning("Turso: schema create failed (transport)")
        return

    # v1.14: ALSO bootstrap the shared token-bucket table so both bots
    # write to the SAME Turso collection. BOT 0 uses this as the primary
    # store (with Mongo as fallback); BOT 1 historically used a separate
    # Mongo collection `nhentai_bucket` and the two processes never saw
    # each other's consumption. After this bootstrap, both bots consume
    # from `nhentai_ratelimit` on Turso and the shared quota is enforced
    # consistently.
    create_rl_sql = (
        'CREATE TABLE IF NOT EXISTS nhentai_ratelimit ('
        '"bucket_id" TEXT PRIMARY KEY, '
        'tokens REAL NOT NULL, '
        'capacity REAL NOT NULL, '
        'rate_per_sec REAL NOT NULL, '
        'updated_at REAL NOT NULL'
        ')'
    )
    res_rl = await _pipeline([{"sql": create_rl_sql}])
    if res_rl is None:
        log.warning("Turso: ratelimit schema create failed (transport)")
        return
    log.info("Turso: schema OK")
    await _discover_columns()


async def execute(sql: str, args: Optional[List[Any]] = None) -> Optional[dict]:
    """Generic one-shot statement execution (used by the shared token bucket).

    Returns the raw libsql result dict for the first statement. Args are
    converted into libsql's {type, value} shape — primitives only.
    """
    if not turso_available():
        return None
    stmt_args = []
    for a in (args or []):
        if a is None:
            stmt_args.append({"type": "null"})
        elif isinstance(a, bool):
            stmt_args.append({"type": "integer", "value": "1" if a else "0"})
        elif isinstance(a, int):
            stmt_args.append({"type": "integer", "value": str(a)})
        elif isinstance(a, float):
            # v1.17: libsql rejects stringified floats ("expected f64").
            stmt_args.append({"type": "float",   "value": float(a)})
        else:
            stmt_args.append({"type": "text",    "value": str(a)})
    res = await _pipeline([{"sql": sql, "args": stmt_args}])
    if not res:
        return None
    return res[0]


async def get(key: str) -> Optional[dict]:
    if not turso_available():
        return None
    # We only care about payload + expires_at for reads; select them if
    # they exist, else fall back to just payload.
    if not _SCHEMA_READY:
        await _discover_columns()
    # Build SELECT list from discovered columns so a missing expires_at
    # column doesn't blow up the query.
    wanted = [c for c in ("payload", "expires_at", "updated_at", "cached_at")
              if c in _COLS] or ["payload"]
    sel = ", ".join(wanted)
    res = await _pipeline([{
        "sql": f'SELECT {sel} FROM nhentai_cache WHERE "key" = ?',
        "args": [{"type": "text", "value": str(key)}],
    }])
    if not res:
        return None
    result = res[0] or {}
    cols = result.get("cols") or []
    rows = result.get("rows") or []
    if not rows:
        return None
    named = _row_named(cols, rows[0])
    payload_raw = named.get("payload")
    if payload_raw is None:
        return None
    try:
        payload = json.loads(payload_raw)
    except Exception:
        return None
    return {
        "payload": payload,
        "updated_at": int(named.get("updated_at") or 0),
        "expires_at": int(named.get("expires_at") or 0),
        "cached_at":  int(named.get("cached_at") or 0),
    }


async def put(key: str, payload: Any, ttl_sec: int) -> bool:
    """Schema-proof INSERT.

    Writes every column that exists in the table:
      * key         — the cache key
      * payload     — JSON blob
      * cached_at   — first-write epoch (== now for new rows, unchanged on update)
      * expires_at  — now + ttl_sec
      * ttl_sec     — the TTL used
      * updated_at  — now (always refreshed)

    If BOT 0 later adds another NOT NULL column, we log which one and stop
    silently succeeding a broken write.
    """
    if not turso_available():
        return False
    if not _SCHEMA_READY:
        await _discover_columns()
    if not _COLS:
        # PRAGMA came back empty — table probably doesn't exist; fall back
        # to the CREATE path and retry once.
        await bootstrap_schema()
        if not _COLS:
            log.warning("Turso: schema still unknown, refusing write for %s", key)
            return False

    now = int(time.time())
    ttl = int(ttl_sec)
    body = json.dumps(payload, ensure_ascii=False)

    # v1.12: ttl_sec == 0 is the never-expire sentinel. Store expires_at=0
    # verbatim so BOT 0's nhentai_cache.py (v12.20) recognises the row as
    # always-fresh. Non-zero ttl behaves as before: expires_at = now + ttl.
    expires_at_val = 0 if ttl == 0 else (now + ttl)

    # Full value table — every column BOT 0's schema has ever used.
    values: Dict[str, Any] = {
        "key":        {"type": "text",    "value": str(key)},
        "payload":    {"type": "text",    "value": body},
        "cached_at":  {"type": "integer", "value": str(now)},
        "expires_at": {"type": "integer", "value": str(expires_at_val)},
        "ttl_sec":    {"type": "integer", "value": str(ttl)},
        "updated_at": {"type": "integer", "value": str(now)},
    }

    # Filter to columns that actually exist.
    active_cols = [c for c in ("key", "payload", "cached_at", "expires_at",
                               "ttl_sec", "updated_at") if c in _COLS]
    if "key" not in active_cols or "payload" not in active_cols:
        log.warning("Turso: table missing required columns key/payload")
        return False

    # Alert loudly if the table has a NOT NULL column we don't know how to
    # fill — future-proofing against BOT 0 adding another mandatory column.
    unknown_not_null = _NOT_NULL - set(values.keys())
    if unknown_not_null:
        log.warning(
            "Turso: table has NOT NULL columns we can't fill: %s — "
            "add them to turso_client._DEFAULTS_ or the write will fail.",
            sorted(unknown_not_null),
        )

    col_list = ", ".join(f'"{c}"' for c in active_cols)
    placeholders = ", ".join("?" for _ in active_cols)

    # ON CONFLICT: update everything except cached_at (that's when the row
    # was first cached — preserve the original value).
    update_cols = [c for c in active_cols
                   if c not in ("key", "cached_at")]
    update_set = ", ".join(f'"{c}" = excluded."{c}"' for c in update_cols)

    sql = (
        f'INSERT INTO nhentai_cache ({col_list}) VALUES ({placeholders}) '
        f'ON CONFLICT("key") DO UPDATE SET {update_set}'
    )
    args = [values[c] for c in active_cols]
    res = await _pipeline([{"sql": sql, "args": args}])
    if not res:
        return False
    # Empty dict in position 0 means the stmt errored (see _pipeline).
    return bool(res[0])


async def close() -> None:
    global _client
    with _client_lock:
        c = _client
        _client = None
    if c is not None:
        try:
            await c.aclose()
        except Exception:  # noqa: BLE001
            pass
