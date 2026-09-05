"""
turso_backup.py — v1.28: Turso -> second-Mongo backup + restore.

Why: Turso free tier hard-locks the account at 500M reads/month. If that
ever fires while Ryan is away, the whole cache (nhentai_cache + Bot 2
state) would be unreachable. This mirrors every row into a SEPARATE Mongo
cluster (different account from the main one) so disaster recovery is a
single --restore run.

Reads: ONE batch SELECT per 500 rows via the keyset cursor (key > :last) —
a full pass over ~19k rows costs ~19k Turso reads, i.e. ~38k/day at the
default 12h cadence (0.2% of budget — safe now that the scan storm is
fixed). Mongo side uses bulk upserts; the second cluster is not read-
metered.

Env (Render, ScraperBot service):
  2NDMONGO_BACKUP_TURSO   — second-cluster Mongo URI (required; the
                            user's intended name "2ndmongo_backup_turso"
                            is matched case-insensitively; MONGO2_BACKUP_URI
                            is also accepted as a fallback name)
  MONGO2_BACKUP_DB        — database name (default "turso_backup")
  BACKUP_EVERY_HOURS      — cadence (default 12)
  BACKUP_BATCH            — rows per Turso SELECT (default 500)
  BACKUP_ENABLED          — "0" disables the loop (default on)

Restore (manual, from a PC):
  python -m app.services.turso_backup --restore
  (needs 2NDMONGO_BACKUP_TURSO + TURSO_DATABASE_URL/TURSO_AUTH_TOKEN set)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .. import mongo_client, turso_client

log = logging.getLogger("scraperbot.turso_backup")

_TABLES = ("nhentai_cache", "bot2_fetch_state", "bot2_done")
_K_STATE = "turso_backup_state"          # scraper1_state doc id
_DEFAULT_EVERY_H = 12


def _mongo2_uri() -> str:
    # Render env names are case-sensitive but ops typos aren't — check the
    # three plausible spellings.
    for name in ("2NDMONGO_BACKUP_TURSO", "2ndmongo_backup_turso",
                 "MONGO2_BACKUP_URI"):
        v = (os.getenv(name) or "").strip()
        if v:
            return v
    return ""


def _mongo2_db() -> str:
    return (os.getenv("MONGO2_BACKUP_DB") or "turso_backup").strip() or "turso_backup"


def _every_sec() -> float:
    try:
        h = float(os.getenv("BACKUP_EVERY_HOURS", str(_DEFAULT_EVERY_H)) or _DEFAULT_EVERY_H)
        return max(600.0, h * 3600.0)
    except (TypeError, ValueError):
        return _DEFAULT_EVERY_H * 3600.0


def _batch_size() -> int:
    try:
        return max(50, int(os.getenv("BACKUP_BATCH", "500") or 500))
    except (TypeError, ValueError):
        return 500


def _mongo2_client():
    from pymongo import MongoClient
    return MongoClient(_mongo2_uri(), serverSelectionTimeoutMS=15000)


def _enabled() -> bool:
    return (os.getenv("BACKUP_ENABLED", "1").strip().lower()
            not in ("0", "false", "no", "off"))


async def backup_once() -> Dict[str, Any]:
    """One full backup pass over all tables. Returns a summary dict."""
    if not _mongo2_uri():
        log.warning("turso_backup: 2NDMONGO_BACKUP_TURSO not set — skipping")
        return {"ok": False, "reason": "no-uri"}
    if not turso_client.turso_available():
        log.warning("turso_backup: Turso unavailable — skipping")
        return {"ok": False, "reason": "turso-down"}

    cli = await asyncio.to_thread(_mongo2_client)
    db = cli[_mongo2_db()]
    batch = _batch_size()
    summary: Dict[str, Any] = {"ok": True, "tables": {}, "started": time.time()}

    for table in _TABLES:
        coll = db[f"turso_{table}"]
        last_key = ""
        copied = 0
        while True:
            # keyset pagination — PK-ordered, no OFFSET scans
            res = await turso_client.execute(
                f'SELECT * FROM {table} WHERE "key" > ? ORDER BY "key" LIMIT ?',
                [last_key, batch])
            if res is None:
                summary["ok"] = False
                summary["reason"] = f"turso-read-failed:{table}"
                break
            rows = res.get("rows") or []
            if not rows:
                break
            cols = [c.get("name") for c in (res.get("cols") or [])]

            def _cell(v):
                return v.get("value") if isinstance(v, dict) else v

            docs = []
            for r in rows:
                vals = [(_cell(x) if isinstance(x, (dict,)) else x) for x in r]
                doc = {cols[i]: vals[i] for i in range(len(cols))} if cols else {}
                k = str(doc.get("key") or "")
                if not k:
                    continue
                doc["_id"] = k
                docs.append(doc)
                last_key = k
            if docs:
                def _bulk(d=docs, c=coll):
                    from pymongo import ReplaceOne
                    c.bulk_write([ReplaceOne({"_id": d0["_id"]}, d0, upsert=True)
                                  for d0 in d], ordered=False)
                await asyncio.to_thread(_bulk)
                copied += len(docs)
            await asyncio.sleep(0.25)   # gentle on Turso reads
        summary["tables"][table] = copied
        log.info("turso_backup: %s -> %d rows", table, copied)

    summary["finished"] = time.time()
    try:
        mongo_client.state_set(_K_STATE, {
            "last_ok": summary["finished"] if summary["ok"] else 0,
            "tables": summary["tables"],
            "reason": summary.get("reason", ""),
        })
    except Exception:  # noqa: BLE001
        pass
    try:
        cli.close()
    except Exception:  # noqa: BLE001
        pass
    log.info("turso_backup: done ok=%s tables=%s", summary["ok"], summary["tables"])
    return summary


async def run_forever(stop_event: asyncio.Event) -> None:
    """Loop: backup every BACKUP_EVERY_HOURS (default 12h)."""
    log.info("turso_backup: starting (every=%.1fh, enabled=%s)",
             _every_sec() / 3600.0, _enabled())
    while not stop_event.is_set():
        if _enabled():
            try:
                await backup_once()
            except Exception as e:  # noqa: BLE001
                log.exception("turso_backup: unhandled: %s", e)
        else:
            log.info("turso_backup: BACKUP_ENABLED=0 — idling")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_every_sec())
            break
        except asyncio.TimeoutError:
            pass
    log.info("turso_backup: stopped")


def status() -> Dict[str, Any]:
    st = mongo_client.state_get(_K_STATE, {}) or {}
    st["enabled"] = _enabled()
    st["uri_set"] = bool(_mongo2_uri())
    st["every_hours"] = _every_sec() / 3600.0
    return st


# ---------------------------------------------------------------------------
# Manual restore: python -m app.services.turso_backup --restore
# ---------------------------------------------------------------------------
async def _restore_async() -> None:
    if not _mongo2_uri():
        print("Set 2NDMONGO_BACKUP_TURSO first."); return
    cli = _mongo2_client()
    db = cli[_mongo2_db()]
    for table in _TABLES:
        coll = db["turso_" + table]
        n = 0
        cursor = coll.find({}, batch_size=500)
        for doc in cursor:
            doc = dict(doc)
            k = doc.pop("_id", None)
            if not k:
                continue
            cols = [c for c in doc.keys() if c != "key"]
            col_sql = ", ".join('"key"' if c == "key" else '"%s"' % c
                                for c in (["key"] + cols))
            ph = ", ".join("?" for _ in (["key"] + cols))
            sql = ('INSERT OR REPLACE INTO ' + table
                   + ' (' + col_sql + ') VALUES (' + ph + ')')
            args = [k] + [doc[c] for c in cols]
            await turso_client.execute(sql, args)
            n += 1
            if n % 500 == 0:
                print("  %s: %d restored..." % (table, n), flush=True)
        print("%s: %d rows restored" % (table, n))
    cli.close()
    print("RESTORE DONE")


if __name__ == "__main__":
    import sys
    if "--restore" in sys.argv:
        asyncio.run(_restore_async())
    else:
        print("turso_backup module — runs as a service task. "
              "Use --restore for manual disaster recovery.")
