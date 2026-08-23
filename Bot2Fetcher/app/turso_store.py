"""
turso_store.py — shared Turso access via the raw HTTP /v2/pipeline API.

v12.40d: same paged-scan structure as v12.40c, but with emoji logging
throughout so the Render log shows the pipeline working at a glance.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("bot2fetcher.turso")

STATE_TABLE = "bot2_fetch_state"
_MAX_SEARCH_ROWS = 200


def _normalise_url(raw: str) -> str:
    u = (raw or "").strip()
    if u.startswith("turso://"):
        u = "https://" + u[len("turso://"):]
    elif u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    elif "://" not in u:
        u = "https://" + u
    return u.rstrip("/")


def _arg(v: Any) -> Dict[str, Any]:
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


def _cell(c: Any) -> Any:
    if c is None:
        return None
    if not isinstance(c, dict):
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


class Turso:
    def __init__(self, url: str, token: str):
        self.base = _normalise_url(url)
        self.endpoint = self.base + "/v2/pipeline"
        self.token = token
        self._ready = False

    async def _pipeline(self, sql: str, args: Optional[list] = None) -> Optional[Dict[str, Any]]:
        body = {
            "requests": [
                {"type": "execute",
                 "stmt": {"sql": sql,
                          "args": [_arg(a) for a in (args or [])]}},
                {"type": "close"},
            ]
        }
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=45.0) as h:
                r = await h.post(self.endpoint, headers=headers, json=body)
            if r.status_code != 200:
                log.warning("🚨 turso HTTP %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
        except Exception as e:
            log.warning("🚨 turso request failed (%s): %s", sql.split(None, 1)[0], e)
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

    async def execute(self, sql: str, args: Optional[list] = None) -> Optional[Dict[str, Any]]:
        result = await self._pipeline(sql, args)
        if result is None:
            return None
        cols = [c.get("name") for c in (result.get("cols") or [])]
        rows_out: List[Dict[str, Any]] = []
        for row in result.get("rows") or []:
            d: Dict[str, Any] = {}
            for i, cell in enumerate(row):
                key = cols[i] if i < len(cols) else str(i)
                d[key] = _cell(cell)
            rows_out.append(d)
        return {"columns": cols, "rows": rows_out}

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        await self.execute(
            f'CREATE TABLE IF NOT EXISTS {STATE_TABLE} ('
            '"key" TEXT PRIMARY KEY, payload TEXT NOT NULL, '
            'updated_at INTEGER NOT NULL DEFAULT 0)'
        )
        self._ready = True

    async def list_gallery_ids(self) -> List[Dict[str, Any]]:
        result = await self.execute(
            'SELECT "key", cached_at FROM nhentai_cache '
            "WHERE \"key\" LIKE 'gallery:%'"
        )
        if not result:
            log.warning("🚨 list_gallery_ids: execute returned None")
            return []
        out: List[Dict[str, Any]] = []
        for r in result["rows"]:
            k = r.get("key") or ""
            gid = k.split(":", 1)[1] if ":" in k else ""
            if not gid.isdigit():
                continue
            try:
                ca = int(r.get("cached_at") or 0)
            except (TypeError, ValueError):
                ca = 0
            out.append({"gid": gid, "cached_at": ca})
        log.info("📚 list_gallery_ids: %d galleries in Turso cache", len(out))
        return out

    async def list_recent_search_ids(self) -> List[str]:
        result = await self.execute(
            'SELECT "key", payload FROM nhentai_cache '
            "WHERE \"key\" LIKE 'search:%' AND \"key\" LIKE '%recent%' "
            "LIMIT ?", [_MAX_SEARCH_ROWS]
        )
        if not result:
            return []
        pages: List[tuple] = []
        for r in result["rows"]:
            k = r.get("key") or ""
            payload_raw = r.get("payload")
            if not k or payload_raw is None:
                continue
            try:
                if isinstance(payload_raw, (bytes, bytearray)):
                    payload_raw = payload_raw.decode("utf-8", "ignore")
                payload = json.loads(payload_raw)
            except Exception:
                continue
            page = 0
            tail = k.rsplit("page", 1)[-1]
            try:
                page = int("".join(ch for ch in tail if ch.isdigit()) or 0)
            except ValueError:
                page = 0
            pages.append((page, payload))
        pages.sort(key=lambda t: t[0])
        ids: List[str] = []
        for _, payload in pages:
            items = payload.get("result") if isinstance(payload, dict) else None
            if isinstance(items, list):
                for it in items:
                    gid = str((it or {}).get("id") or "")
                    if gid.isdigit():
                        ids.append(gid)
        log.info("🔥 list_recent_search_ids: %d ids from %d recent pages",
                 len(ids), len(pages))
        return ids

    async def get_gallery_row(self, gid: str) -> Optional[Dict[str, Any]]:
        result = await self.execute(
            'SELECT payload FROM nhentai_cache WHERE "key" = ?',
            [f"gallery:{gid}"])
        if not result or not result["rows"]:
            log.info("🔍 gallery:%s — no row in Turso", gid)
            return None
        raw = result["rows"][0].get("payload")
        if raw is None:
            log.warning("🔍 gallery:%s — row found but payload is NULL", gid)
            return None
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            parsed = json.loads(raw)
            log.debug("🔍 gallery:%s — payload parsed, keys=%s", gid, list(parsed.keys())[:8])
            return {"payload": parsed}
        except Exception as e:
            log.warning("🔍 gallery:%s — payload JSON parse failed: %s", gid, e)
            return None

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
