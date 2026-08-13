"""
turso_client.py — Turso client using the raw HTTP API (v2/pipeline).

v1.2 rewrite: drops libsql-client entirely. libsql-client 0.3.x returns
different result shapes depending on whether the URL is libsql:// (WS) or
https:// (HTTP), and yielded `'result'` KeyErrors against your https
Turso endpoint on every read/write.

The HTTP API is stable, documented, and identical to what BOT 0's
turso_client uses at deploy time:

    POST <TURSO_DATABASE_URL>/v2/pipeline
    Authorization: Bearer <TURSO_AUTH_TOKEN>
    { "requests": [
        {"type":"execute","stmt":{"sql":"…","args":[…]}},
        {"type":"close"}
    ]}

Response shape:
    { "results": [
        {"type":"ok","response":{"type":"execute","result":{
            "cols":[{"name":"payload"},{"name":"updated_at"},…],
            "rows":[[{"type":"text","value":"…"}, …]]
        }}},
        {"type":"ok","response":{"type":"close"}}
    ]}

We normalise `libsql://` to `https://` for backward compat and use httpx
so there is exactly one dependency shape across the whole file.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, List, Optional

import httpx

from .config import settings

log = logging.getLogger("scraperbot.turso")


def _http_base_url() -> str:
    """Normalise the configured URL to the HTTP form the pipeline API needs."""
    url = (settings.turso_url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("libsql://"):
        return "https://" + url[len("libsql://"):]
    return url


_client_lock = threading.Lock()
_client: Optional[httpx.AsyncClient] = None


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
    """Run one or more statements through /v2/pipeline. Returns the list of
    per-statement `result` dicts (only for successful `execute` responses),
    or None on transport / auth failure."""
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
        etype = entry.get("type")
        if etype == "error":
            log.warning("Turso pipeline stmt error: %s",
                        (entry.get("error") or {}).get("message"))
            out.append({})
            continue
        resp = entry.get("response") or {}
        if resp.get("type") == "execute":
            out.append(resp.get("result") or {})
    return out


def _row_value(cell: Any) -> Any:
    """The HTTP API wraps every cell as {'type': 'text'|'integer'|'null',
    'value': '…'}. Extract the raw value; also tolerates plain values from
    older API responses."""
    if isinstance(cell, dict):
        t = cell.get("type")
        v = cell.get("value")
        if t == "null" or v is None:
            return None
        if t == "integer":
            try:
                return int(v)
            except (TypeError, ValueError):
                return v
        if t == "float":
            try:
                return float(v)
            except (TypeError, ValueError):
                return v
        return v
    return cell


def _row_named(cols: list, row: list) -> dict:
    """Build a {col_name: value} dict from a pipeline row."""
    out: dict = {}
    for i, c in enumerate(cols or []):
        name = c.get("name") if isinstance(c, dict) else str(c)
        if not name:
            name = f"col_{i}"
        val = row[i] if i < len(row) else None
        out[name] = _row_value(val)
    return out


async def bootstrap_schema() -> None:
    """CREATE TABLE IF NOT EXISTS. Idempotent; safe every boot."""
    if not turso_available():
        log.info("Turso not configured — skipping schema bootstrap")
        return
    sql = ("CREATE TABLE IF NOT EXISTS nhentai_cache ("
           "key TEXT PRIMARY KEY, "
           "payload TEXT NOT NULL, "
           "updated_at INTEGER NOT NULL, "
           "expires_at INTEGER NOT NULL)")
    res = await _pipeline([{"sql": sql}])
    if res is not None:
        log.info("Turso: schema OK")


async def get(key: str) -> Optional[dict]:
    if not turso_available():
        return None
    res = await _pipeline([{
        "sql": "SELECT payload, updated_at, expires_at "
               "FROM nhentai_cache WHERE key = ?",
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
    }


async def put(key: str, payload: Any, ttl_sec: int) -> bool:
    if not turso_available():
        return False
    now = int(time.time())
    body = json.dumps(payload, ensure_ascii=False)
    sql = ("INSERT INTO nhentai_cache (key, payload, updated_at, expires_at) "
           "VALUES (?, ?, ?, ?) "
           "ON CONFLICT(key) DO UPDATE SET "
           "  payload    = excluded.payload, "
           "  updated_at = excluded.updated_at, "
           "  expires_at = excluded.expires_at")
    res = await _pipeline([{
        "sql": sql,
        "args": [
            {"type": "text",    "value": str(key)},
            {"type": "text",    "value": body},
            {"type": "integer", "value": str(now)},
            {"type": "integer", "value": str(now + int(ttl_sec))},
        ],
    }])
    return res is not None


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
