"""
purge_mongo_nhentai_cache.py — v12.34d one-shot maintenance script.

Purpose
-------
The `nhentai_cache` collection in MongoDB is a legacy read-fallback from
the pre-v12.4 era. Since v12.4:

  * BOT 0 writes: gated OFF (`BOT0_NH_MONGO_WRITES=0` default)
  * BOT 1 writes: gated OFF (`BOT1_CACHE_MONGO_MIRROR=0` default)
  * BOTH bots READ Turso first, Mongo only as fallback.

The rows still sitting in that collection are pre-v12.4 leftovers with
naive-datetime `expires_at` values. Every time BOT 0's `_turso_get()`
returns None (any transport race, replica lag, network glitch, ...),
`nhentai_cache.get()` falls through to `_mongo_get()` which USED to
crash on `TypeError: can't compare offset-naive and offset-aware
datetimes`. v12.34d makes the read path safe against that garbage; this
purge script removes the garbage entirely so the fallback is a clean
"no row exists" instead of "row exists but is unusable."

Usage
-----
  MONGO_URI="mongodb+srv://..."  python scripts/purge_mongo_nhentai_cache.py
  MONGO_URI="..." DRY_RUN=1      python scripts/purge_mongo_nhentai_cache.py

Safety
------
- DRY_RUN=1 (or --dry-run) counts + samples but writes nothing.
- Deletion targets the `nhentai_cache` collection ONLY. Never touches
  `queue`, `galleries`, `processed_urls`, `scraper1_state`, etc.
- Idempotent: run it once, run it 100 times, same terminal state.
- Prints the collection size before + after so you can sanity-check the
  Render dashboard "storage used" chart after the run.

Exit codes
----------
0 = success (including dry-run)
1 = MONGO_URI missing
2 = mongo error
"""
from __future__ import annotations

import os
import sys
from typing import Optional

DEFAULT_DB_NAME = "relaybot"
COLLECTION = "nhentai_cache"


def _mongo_uri() -> Optional[str]:
    uri = (os.environ.get("MONGO_URI") or os.environ.get("MONGODB_URI") or "").strip()
    return uri or None


def _db_name() -> str:
    return (os.environ.get("MONGO_DB_NAME") or DEFAULT_DB_NAME).strip() or DEFAULT_DB_NAME


def _is_dry_run() -> bool:
    if "--dry-run" in sys.argv:
        return True
    val = (os.environ.get("DRY_RUN") or "").strip().lower()
    return val in ("1", "true", "yes", "on")


def main() -> int:
    uri = _mongo_uri()
    if not uri:
        print("ERROR: MONGO_URI is not set", file=sys.stderr)
        return 1

    try:
        from pymongo import MongoClient
    except ImportError:
        print("ERROR: pymongo is not installed. Run: pip install pymongo", file=sys.stderr)
        return 2

    dry_run = _is_dry_run()
    db_name = _db_name()

    print(f"purge_mongo_nhentai_cache: db={db_name} coll={COLLECTION} dry_run={dry_run}")

    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: mongo connect failed: {e}", file=sys.stderr)
        return 2

    coll = client[db_name][COLLECTION]

    try:
        before = coll.estimated_document_count()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: count failed: {e}", file=sys.stderr)
        return 2

    print(f"  rows before: {before}")
    if before == 0:
        print("  nothing to purge; exiting clean")
        return 0

    # Sample a handful so the operator can eyeball WHICH keys are being
    # deleted. Purely informational; no filtering happens on the delete.
    try:
        sample = list(coll.find({}, {"_id": 1, "expires_at": 1}).limit(5))
        print("  sample (first 5 keys):")
        for doc in sample:
            print(f"    _id={doc.get('_id')!r}  expires_at={doc.get('expires_at')!r}")
    except Exception as e:  # noqa: BLE001
        print(f"  sample failed (non-fatal): {e}")

    if dry_run:
        print(f"  DRY_RUN=1: would delete {before} rows; no writes performed")
        return 0

    try:
        result = coll.delete_many({})
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: delete_many failed: {e}", file=sys.stderr)
        return 2

    try:
        after = coll.estimated_document_count()
    except Exception:  # noqa: BLE001
        after = -1

    print(f"  deleted: {result.deleted_count}")
    print(f"  rows after: {after}")
    print("purge_mongo_nhentai_cache: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
