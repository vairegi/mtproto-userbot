"""
turso_client.py — v12.46: raw HTTP /v2/pipeline rewrite.

WHY THIS REWRITE (root cause of "Mini App loads from nhentai instead of
Turso" on 2026-08-26):

  The old client drove Turso through the `libsql-client` Python package —
  the same driver the handover documented as broken on Render for this
  database ("KeyError: 'result' inside the driver", first seen on
  Bot2Fetcher v12.40). Both other bots (ScraperBot, Bot2Fetcher) already
  use raw HTTP /v2/pipeline via httpx; BOT 0 was the last holdout.

  Failure signature, verified against the live DB:
    * BOT 0 logged "📝 [TURSO WRITE] ... key=search:popular-today:page1"
      but the row's cached_at in Turso stayed at its BOT-1 sweep
      timestamp — the write never landed.
    * BOT 0 then read the same key and saw NOTHING (execute() returned
      None through the broken driver), logged "[CACHE MISS]", and hit
      nhentai again — every request, every page, exactly what Ryan saw.
    * Meanwhile BOT 1 kept writing the same keys every few minutes over
      raw HTTP without a single failure. Same DB, same network — only
      the driver differed.

PUBLIC API — unchanged, so every caller keeps working untouched:
    turso_available() -> bool
    execute(sql, args=None) -> ResultSet-like (has .rows, positional cells)
    ensure_schema() -> bool
    health() -> dict

The ResultSet shim is a SimpleNamespace(rows=[list, ...]) — every caller
(nhentai_cache, scraper_bridge) only uses rs.rows and row[i] positional
access, which plain lists satisfy.

Threading model: execute() is SYNC (httpx.Client), callable from any
thread. The old asyncio bridge machinery (_run / _run_via_bridge /
_run_in_fresh_loop) is deleted — nothing outside this module used it.

Environment (unchanged):
    TURSO_DATABASE_URL   libsql:// or https:// or bare host — normalised
    TURSO_AUTH_TOKEN     JWT from the Turso dashboard

Failure semantics (unchanged): every public function catches everything
and returns None / False — Turso is an accelerator, never a hard dep.
"""
from __future__ import annotations

import logging
import os
import threading
import time as _time
from types import SimpleNamespace
from typing import Any, List, Optional

import httpx

log = logging.getLogger("miniapp.turso")

# ---------------------------------------------------------------------------
# Env + URL normalisation (same behaviour as the old module: accept
# libsql://, turso://, https:// or a bare hostname; always end on https://)
# ---------------------------------------------------------------------------
_TURSO_URL_RAW = os.environ.get("TURSO_DATABASE_URL", "").strip()
_TURSO_TOKEN   = os.environ.get("TURSO_AUTH_TOKEN",   "").strip()


def _normalize_turso_url(raw: str) -> tuple[str, Optional[str]]:
    """Return (fixed_url, note). Always https:// (raw HTTP pipeline)."""
    u = (raw or "").strip()
    note = None
    if not u:
        return "", "TURSO_DATABASE_URL unset"
    for scheme in ("libsql://", "turso://", "wss://", "ws://"):
        if u.startswith(scheme):
            u = "https://" + u[len(scheme):]
            note = f"scheme normalised from {scheme} to https://"
            break
    if "://" not in u:
        u = "https://" + u
        note = "bare hostname — prefixed with https://"
    return u.rstrip("/"), note


_TURSO_URL, _TURSO_URL_NOTE = _normalize_turso_url(_TURSO_URL_RAW)
_PIPELINE_URL = _TURSO_URL + "/v2/pipeline" if _TURSO_URL else ""

_schema_ready = False
_schema_lock = threading.Lock()


def turso_available() -> bool:
    """True iff both env vars are present. Cheap, side-effect free."""
    return bool(_PIPELINE_URL) and bool(_TURSO_TOKEN)


# ---------------------------------------------------------------------------
# Raw /v2/pipeline transport — the exact same protocol ScraperBot and
# Bot2Fetcher have been running in production without a single driver bug.
# ---------------------------------------------------------------------------
def _arg_encode(v: Any) -> dict:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        import base64
        return {"type": "blob",
                "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _cell_decode(c: Any) -> Any:
    if c is None or not isinstance(c, dict):
        return c
    t = c.get("type")
    if t == "null":
        return None
    if t == "integer":
        try:
            return int(c.get("value") or 0)
        except (TypeError, ValueError):
            return 0
    if t == "float":
        try:
            return float(c.get("value") or 0)
        except (TypeError, ValueError):
            return 0.0
    if t == "blob":
        import base64
        try:
            return base64.b64decode(c.get("base64") or "")
        except Exception:
            return b""
    return c.get("value")


def _pipeline(sql: str, args: Optional[list] = None) -> Optional[dict]:
    """One statement via POST /v2/pipeline. Returns the raw libsql `result`
    dict {"cols": [...], "rows": [...]} or None on any failure. Loud-logs
    the server-side error message (this is what libsql-client swallowed)."""
    if not turso_available():
        return None
    body = {"requests": [
        {"type": "execute",
         "stmt": {"sql": sql,
                  "args": [_arg_encode(a) for a in (args or [])]}},
        {"type": "close"},
    ]}
    headers = {"Authorization": f"Bearer {_TURSO_TOKEN}",
               "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=20.0) as c:
            r = c.post(_PIPELINE_URL, headers=headers, json=body)
        if r.status_code != 200:
            log.warning("turso: HTTP %s on %s: %s",
                        r.status_code, sql.split(None, 1)[0], r.text[:200])
            return None
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.warning("turso: transport error on %s: %s",
                    sql.split(None, 1)[0], e)
        return None
    results = data.get("results") or []
    if not results:
        log.warning("turso: empty results for %s: %s",
                    sql.split(None, 1)[0], str(data)[:200])
        return None
    first = results[0]
    if first.get("type") != "ok":
        err = first.get("error") or first
        log.warning("turso: stmt error on %s: %s",
                    sql.split(None, 1)[0], str(err)[:200])
        return None
    return (first.get("response") or {}).get("result") or {}


def execute(sql: str, args: Optional[list] = None):
    """Run one SQL statement synchronously.

    Returns a ResultSet-like SimpleNamespace(rows=[list-of-cells, ...])
    with cells in SELECT column order — positional access (row[0], row[1])
    works exactly like the old libsql-client rows. None on any error;
    callers already treat None as "fall back to Mongo / fetch upstream".
    """
    if not turso_available():
        return None
    result = _pipeline(sql, args)
    if result is None:
        return None
    rows: List[list] = []
    for row in result.get("rows") or []:
        rows.append([_cell_decode(cell) for cell in row])
    return SimpleNamespace(rows=rows)


# ---------------------------------------------------------------------------
# Schema bootstrap (idempotent, same DDL as before)
# ---------------------------------------------------------------------------
_DDL_STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS nhentai_cache (
        key         TEXT    PRIMARY KEY,
        payload     TEXT    NOT NULL,
        cached_at   INTEGER NOT NULL,
        expires_at  INTEGER NOT NULL,
        ttl_sec     INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_nhcache_expires ON nhentai_cache(expires_at)",
    """CREATE TABLE IF NOT EXISTS nhentai_ratelimit (
        bucket_id     TEXT    PRIMARY KEY,
        tokens        REAL    NOT NULL,
        capacity      INTEGER NOT NULL,
        rate_per_sec  REAL    NOT NULL,
        updated_at    REAL    NOT NULL
    )""",
]


def ensure_schema() -> bool:
    """Create the two Turso tables + indexes if they don't exist.
    Safe to call repeatedly; the first success short-circuits."""
    global _schema_ready
    if _schema_ready:
        return True
    if not turso_available():
        return False
    with _schema_lock:
        if _schema_ready:
            return True
        for stmt in _DDL_STATEMENTS:
            if _pipeline(stmt) is None:
                log.warning("turso: ensure_schema failed on: %s",
                            stmt.split("(")[0])
                return False
        _schema_ready = True
        log.info("turso: schema ready (nhentai_cache + nhentai_ratelimit) "
                 "via raw HTTP /v2/pipeline")
        return True


# ---------------------------------------------------------------------------
# Health + diagnostics (used by admin /prefetch status, /diag)
# ---------------------------------------------------------------------------
def _url_diag() -> dict:
    def _mask(u: str) -> str:
        if not u:
            return "(unset)"
        if "@" in u and "://" in u:
            head, tail = u.split("://", 1)
            if "@" in tail:
                tail = tail.split("@", 1)[1]
            u = f"{head}://{tail}"
        return u
    scheme = _TURSO_URL.split("://", 1)[0].lower() if "://" in _TURSO_URL else ""
    return {
        "raw_masked":   _mask(_TURSO_URL_RAW),
        "fixed_masked": _mask(_TURSO_URL),
        "scheme":       scheme,
        "scheme_ok":    scheme in ("https", "http"),
        "auto_note":    _TURSO_URL_NOTE,
        "token_set":    bool(_TURSO_TOKEN),
        "driver":       "raw-http-v2-pipeline",
    }


def health() -> dict:
    """Diagnostic: is Turso reachable, and how fast?"""
    url = _url_diag()
    if not _TURSO_URL_RAW:
        return {"available": False, "reason": "TURSO_DATABASE_URL not set",
                "url": url}
    if not _TURSO_TOKEN:
        return {"available": False, "reason": "TURSO_AUTH_TOKEN not set",
                "url": url}
    if not url["scheme_ok"]:
        return {"available": False,
                "reason": f"URL scheme {url['scheme']!r} unusable (need https://)",
                "url": url}
    t0 = _time.time()
    rs = execute("SELECT 1")
    dur_ms = int((_time.time() - t0) * 1000)
    ok = bool(rs is not None and rs.rows and rs.rows[0][0] == 1)
    out = {"available": ok, "latency_ms": dur_ms, "url": url}
    if not ok:
        out["reason"] = "pipeline query failed (see WARNING lines in log)"
    return out
