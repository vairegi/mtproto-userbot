"""
turso_store.py — shared Turso (libSQL) access for Bot2Fetcher.

v12.40a fix: libsql_client's ResultSet.rows are dict-like objects, NOT
positional tuples. The v12.40 build did `for k, payload, cached_at in rows`
which raised `KeyError: 'result'` under the hood and made scan return 0
rows. This version normalises every row to a dict AND supports positional
access as a safety net.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("bot2fetcher.turso")

STATE_TABLE = "bot2_fetch_state"


def _normalise_url(raw: str) -> str:
    u = (raw or "").strip()
    if u.startswith("turso://"):
        u = "https://" + u[len("turso://"):]
    elif "://" not in u:
        u = "https://" + u
    return u


def _row_to_dict(row: Any, columns: List[str]) -> Dict[str, Any]:
    """libsql-client Row supports both mapping and sequence access, but the
    behaviour differs between versions. Normalise to a plain dict keyed by
    column name using whichever access works."""
    out: Dict[str, Any] = {}
    # Try mapping access first (newer libsql_client rows).
    try:
        for col in columns:
            out[col] = row[col]
        return out
    except Exception:
        pass
    # Fallback: positional. Rows may be tuple-like OR support .astuple().
    try:
        seq = tuple(row)
        for col, val in zip(columns, seq):
            out[col] = val
        return out
    except Exception:
        pass
    # Last-resort: attribute access on whatever object it is.
    for col in columns:
        try:
            out[col] = getattr(row, col)
        except Exception:
            out[col] = None
    return out


class Turso:
    def __init__(self, url: str, token: str):
        self.url = _normalise_url(url)
        self.token = token
        self._ready = False

    async def _client(self):
        import libsql_client
        return libsql_client.create_client(self.url, auth_token=self.token)

    async def execute(self, sql: str, args: Optional[list] = None):
        """Return {columns, rows_as_dicts} or None on error."""
        client = await self._client()
        try:
            rs = await client.execute(sql, args or [])
            columns = list(getattr(rs, "columns", []) or [])
            raw_rows = list(getattr(rs, "rows", []) or [])
            rows = [_row_to_dict(r, columns) for r in raw_rows]
            return {"columns": columns, "rows": rows}
        except Exception as e:
            log.warning("turso execute failed (%s): %s", sql.split(None, 1)[0], e)
            return None
        finally:
            try:
                await client.close()
            except Exception:
                pass

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        await self.execute(
            f'CREATE TABLE IF NOT EXISTS {STATE_TABLE} ('
            '"key" TEXT PRIMARY KEY, payload TEXT NOT NULL, '
            'updated_at INTEGER NOT NULL DEFAULT 0)'
        )
        self._ready = True

    async def scan_cache(self) -> List[Dict[str, Any]]:
        """Return items: {key, payload(dict), cached_at(int)} for search:*
        and gallery:* rows in nhentai_cache."""
        result = await self.execute(
            'SELECT "key", payload, cached_at FROM nhentai_cache '
            "WHERE \"key\" LIKE 'gallery:%' OR \"key\" LIKE 'search:%'"
        )
        if not result:
            return []
        out: List[Dict[str, Any]] = []
        for r in result["rows"]:
            key = r.get("key") or r.get("KEY") or ""
            payload_raw = r.get("payload")
            cached_at = r.get("cached_at") or 0
            if not key or payload_raw is None:
                continue
            try:
                if isinstance(payload_raw, (bytes, bytearray)):
                    payload_raw = payload_raw.decode("utf-8", "ignore")
                payload = json.loads(payload_raw)
            except Exception:
                continue
            try:
                cached_at = int(cached_at or 0)
            except (TypeError, ValueError):
                cached_at = 0
            out.append({"key": key, "payload": payload, "cached_at": cached_at})
        log.info("scan_cache: read %d rows", len(out))
        return out

    async def put_state(self, key: str, payload: Dict[str, Any]) -> None:
        await self.ensure_schema()
        await self.execute(
            f'INSERT INTO {STATE_TABLE} ("key", payload, updated_at) '
            "VALUES (?, ?, ?) ON CONFLICT(\"key\") DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            [key, json.dumps(payload, separators=(",", ":")), int(time.time())],
        )

    async def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        await self.ensure_schema()
        result = await self.execute(
            f'SELECT payload FROM {STATE_TABLE} WHERE "key" = ?', [key])
        if not result or not result["rows"]:
            return None
        raw = result["rows"][0].get("payload")
        if raw is None:
            return None
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            return json.loads(raw)
        except Exception:
            return None
