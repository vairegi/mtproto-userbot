"""
turso_store.py — shared Turso (libSQL) access for Bot2Fetcher.

READS  : nhentai_cache — the same table BOT 0 and BOT 1 share.
         gallery:<id> payloads carry title/cover/pages/tags (written by
         BOT 1's details sweeper) so we usually don't need a live scrape.
WRITES : bot2_fetch_state — this bot's own progress table ONLY.
         Never writes nhentai_cache, never touches nhentai_ratelimit
         (this bot makes no nhentai API calls on the hot path).

URL scheme self-heal mirrors BOT 0's turso_client v12.6: the dashboard
sometimes hands out turso:// or a bare host; libsql-client needs https://.
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


class Turso:
    def __init__(self, url: str, token: str):
        self.url = _normalise_url(url)
        self.token = token
        self._ready = False

    async def _client(self):
        import libsql_client
        try:
            return libsql_client.create_client(self.url, auth_token=self.token)
        except TypeError:  # older client
            return libsql_client.create_client(self.url, auth_token=self.token)

    async def execute(self, sql: str, args: Optional[list] = None):
        client = await self._client()
        try:
            rs = await client.execute(sql, args or [])
            return list(rs.rows)
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

    # ---- reads from the shared cache ---------------------------------
    async def scan_cache(self) -> List[Dict[str, Any]]:
        """Return rows: key, payload, cached_at for search:* and gallery:*."""
        rows = await self.execute(
            "SELECT key, payload, cached_at FROM nhentai_cache "
            "WHERE key LIKE 'gallery:%' OR key LIKE 'search:%'"
        )
        if rows is None:
            return []
        out = []
        for k, payload, cached_at in rows:
            try:
                out.append({"key": k, "payload": json.loads(payload),
                            "cached_at": int(cached_at or 0)})
            except Exception:
                continue
        return out

    # ---- own state table ----------------------------------------------
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
        rows = await self.execute(
            f'SELECT payload FROM {STATE_TABLE} WHERE "key" = ?', [key])
        if not rows:
            return None
        try:
            return json.loads(rows[0][0])
        except Exception:
            return None
