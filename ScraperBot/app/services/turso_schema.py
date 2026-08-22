"""turso_schema.py — idempotent migration for Bot 0 + Bot 1 + (future) Bot 2."""
from __future__ import annotations
import asyncio, logging
from typing import Iterable
from . import turso_client
log = logging.getLogger("scraperbot.turso_schema")

_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, payload BLOB, expires_at INTEGER NOT NULL);""",
    "CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);",
    """CREATE TABLE IF NOT EXISTS bm_cover (gid TEXT PRIMARY KEY, payload BLOB, expires_at INTEGER NOT NULL);""",
    "CREATE INDEX IF NOT EXISTS idx_bmcover_expires ON bm_cover(expires_at);",
    """CREATE TABLE IF NOT EXISTS bot0_queue_tag (key TEXT PRIMARY KEY, payload BLOB, expires_at INTEGER NOT NULL);""",
    "CREATE INDEX IF NOT EXISTS idx_bot0qtag_expires ON bot0_queue_tag(expires_at);",
    """CREATE TABLE IF NOT EXISTS bucket_replica (bucket TEXT PRIMARY KEY, tokens REAL NOT NULL, updated_at INTEGER NOT NULL);""",
    "CREATE INDEX IF NOT EXISTS idx_bucket_updated ON bucket_replica(updated_at);",
    """CREATE TABLE IF NOT EXISTS bot2_queue (gid TEXT PRIMARY KEY, payload BLOB, expires_at INTEGER NOT NULL);""",
    "CREATE INDEX IF NOT EXISTS idx_bot2q_expires ON bot2_queue(expires_at);",
    """CREATE TABLE IF NOT EXISTS bot2_done (gid TEXT PRIMARY KEY, payload BLOB, expires_at INTEGER NOT NULL);""",
    "CREATE INDEX IF NOT EXISTS idx_bot2d_expires ON bot2_done(expires_at);",
)


async def ensure_schema(extra: Iterable[str] = ()) -> list[str]:
    if not turso_client.turso_available():
        log.warning("turso schema: skipping — TURSO_DATABASE_URL not configured")
        return []
    executed: list[str] = []
    for stmt in (*_SCHEMA, *extra):
        head = stmt.strip().split(None, 1)[0].upper()
        if head not in ("CREATE", "ALTER", "DROP"):
            log.warning("turso schema: refusing non-DDL statement: %r", head)
            continue
        try:
            await turso_client.execute(stmt.replace("\n", " "))
            executed.append(stmt.strip().splitlines()[0].strip())
        except Exception as e:
            log.error("turso schema: %s raised %s", stmt.strip().splitlines()[0].strip(), e)
            raise
    log.info("turso schema: %d statements applied", len(executed))
    return executed


def run_blocking() -> None:
    asyncio.run(ensure_schema())


if __name__ == "__main__":
    run_blocking()
