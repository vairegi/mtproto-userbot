"""
writer.py — v12.47 canonical nhentai_cache SQL builders (PURE: zero I/O).

The builders are the canonical reference for NEW code (the migration
script uses build_update_payload_sql). Existing bot transports keep their
own verified SQL — the cached_at semantics deliberately differ per bot
(BOT 0 refreshes cached_at on rewrite; BOT 1 preserves first-cached time)
and that difference is behavior, not drift. preserve_cached_at covers both.
"""
from __future__ import annotations

import time
from typing import Any, List, Optional, Tuple

_TABLE = "nhentai_cache"


def build_upsert_sql(key: str, payload_json: str, ttl: int,
                     now: Optional[int] = None,
                     preserve_cached_at: bool = False
                     ) -> Tuple[str, List[Any]]:
    """Canonical INSERT..ON CONFLICT upsert. ttl<=0 -> expires_at=0
    (never-expire sentinel both readers honour)."""
    ts = int(now if now is not None else time.time())
    ttl_i = int(ttl or 0)
    expires_at = 0 if ttl_i <= 0 else ts + ttl_i
    stored_ttl = 0 if ttl_i <= 0 else ttl_i
    update = ('payload=excluded.payload, expires_at=excluded.expires_at, '
              'ttl_sec=excluded.ttl_sec')
    if not preserve_cached_at:
        update = 'cached_at=excluded.cached_at, ' + update
    sql = (f'INSERT INTO {_TABLE} ("key", payload, cached_at, expires_at, ttl_sec) '
           f'VALUES (?, ?, ?, ?, ?) ON CONFLICT("key") DO UPDATE SET {update}')
    return sql, [key, payload_json, ts, expires_at, stored_ttl]


def build_update_payload_sql(key: str, payload_json: str
                             ) -> Tuple[str, List[Any]]:
    """Payload-only UPDATE — preserves cached_at/expires_at/ttl_sec.
    Used by the migration script when rewriting legacy rows canonical."""
    return (f'UPDATE {_TABLE} SET payload = ? WHERE "key" = ?',
            [payload_json, key])
