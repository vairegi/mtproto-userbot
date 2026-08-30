"""turso_schema.py — idempotent migration for Bot 0 + Bot 1 + Bot 2.

v12.48 (sync-audit) F8:
  The pre-fix _SCHEMA created six placeholder tables (cache, bm_cover,
  bot0_queue_tag, bucket_replica, bot2_queue, bot2_done) that were never
  written to by any bot. Live audit confirmed all six are 0-row on
  production. They existed for a design that was later replaced by
  `nhentai_cache`, `nhentai_ratelimit`, and `bot2_fetch_state`. Removing
  the DDL keeps the tables that already exist untouched (idempotent
  design) while stopping fresh deploys from re-creating dead schema.

  The active schema owned by other components — `nhentai_cache` is
  bootstrapped by ScraperBot/app/turso_client.py against the live column
  set, `bot2_fetch_state` by Bot2Fetcher/app/turso_store.ensure_schema(),
  `nhentai_ratelimit` by the shared bucket module — is unchanged.

Callers that pass `extra` DDL keep working: their statements run before
the (now empty) built-in schema.
"""
from __future__ import annotations
import asyncio, logging
from typing import Iterable
from app import turso_client
log = logging.getLogger("scraperbot.turso_schema")

# v12.48 (F8): built-in schema is now empty — see module docstring.
# The tuple is preserved (instead of being deleted) so anything that
# imports _SCHEMA by name keeps type-checking; downstream callers using
# `extra=` still work.
_SCHEMA: tuple[str, ...] = ()


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
