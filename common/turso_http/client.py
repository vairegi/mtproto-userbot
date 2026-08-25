"""
client.py — ONE Turso raw-HTTP client for every bot.

Consolidates the three hand-rolled /v2/pipeline implementations
(ScraperBot/app/turso_client.py, miniapp/.../turso_client.py,
Bot2Fetcher/app/turso_store.py) into a single async httpx POST with
consistent retry + error logging. v1.0: behavior-preserving — same
request shape, same response decoding the callers already expect.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("common.turso_http")


def normalise_url(raw: str) -> str:
    u = (raw or "").strip()
    if u.startswith("turso://"):
        u = "https://" + u[len("turso://"):]
    elif u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    elif "://" not in u:
        u = "https://" + u
    return u.rstrip("/")


def arg_encode(v: Any) -> Dict[str, Any]:
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
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def cell_decode(c: Any) -> Any:
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


class TursoHttpClient:
    """Async /v2/pipeline client. One instance per bot, long-lived."""

    def __init__(self, url: str, token: str, timeout: float = 45.0):
        self.base = normalise_url(url)
        self.endpoint = self.base + "/v2/pipeline"
        self.token = token
        self.timeout = timeout

    async def execute_raw(self, sql: str, args: Optional[List[Any]] = None
                          ) -> Optional[Dict[str, Any]]:
        """One statement. Returns the raw libsql `result` dict or None."""
        body = {"requests": [
            {"type": "execute",
             "stmt": {"sql": sql,
                      "args": [arg_encode(a) for a in (args or [])]}},
            {"type": "close"},
        ]}
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as h:
                r = await h.post(self.endpoint, headers=headers, json=body)
            if r.status_code != 200:
                log.warning("🚨 turso HTTP %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
        except Exception as e:
            log.warning("🚨 turso request failed (%s): %s",
                        sql.split(None, 1)[0], e)
            return None
        results = data.get("results") or []
        if not results:
            log.warning("🚨 turso no results: %s", str(data)[:200])
            return None
        first = results[0]
        if first.get("type") != "ok":
            err = first.get("error") or first
            log.warning("🚨 turso stmt error (%s): %s",
                        sql.split(None, 1)[0], str(err)[:200])
            return None
        return (first.get("response") or {}).get("result") or {}

    async def execute(self, sql: str, args: Optional[List[Any]] = None
                      ) -> Optional[Dict[str, Any]]:
        """execute_raw + row decoding into {"columns": [...], "rows": [...]}."""
        result = await self.execute_raw(sql, args)
        if result is None:
            return None
        cols = [c.get("name") for c in (result.get("cols") or [])]
        rows_out: List[Dict[str, Any]] = []
        for row in result.get("rows") or []:
            d: Dict[str, Any] = {}
            for i, cell in enumerate(row):
                key = cols[i] if i < len(cols) else str(i)
                d[key] = cell_decode(cell)
            rows_out.append(d)
        return {"columns": cols, "rows": rows_out}
