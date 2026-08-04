#!/usr/bin/env python3
"""
scripts/migrate_v2_recover_stuck.py — one-shot V2 recovery script.

WHEN TO RUN
-----------
Once, after deploying V2 for the first time on a live database. It:

  1. Finds every `galleries` doc whose `status == "PROCESSING"` older than
     the configured stale threshold (default 15 minutes) or older than an
     override you pass via --older-than-minutes.
  2. Optionally requires --dry-run first, so you can see the count before
     mutating anything.
  3. Marks them `FAILED_RECOVERED` with a `failed_reason` explaining what
     happened, so the dedup gate treats them as retryable tombstones
     instead of "another worker is downloading this now".

This script is IDEMPOTENT: run it as many times as you like. It only ever
touches PROCESSING docs older than the cutoff.

USAGE
-----
    # Preview (safe, no writes):
    python3 scripts/migrate_v2_recover_stuck.py --dry-run

    # Actually recover:
    python3 scripts/migrate_v2_recover_stuck.py

    # Custom cutoff:
    python3 scripts/migrate_v2_recover_stuck.py --older-than-minutes 30

The script exits with:
    0 if it ran cleanly (even if nothing was found),
    2 if Mongo could not be reached / imported.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any, Dict, List

# Make the parent project importable regardless of where this script is run.
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Recover stuck V2 PROCESSING docs.")
    p.add_argument(
        "--older-than-minutes",
        type=int,
        default=15,
        help="Cutoff in minutes. Defaults to MINIAPP_STALE_PROCESSING_S/60 (900s / 15 min).",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only report what would change; do not write.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after N candidates (0 = no limit). Useful on huge backlogs.",
    )
    return p.parse_args()


def _configure_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    return logging.getLogger("migrate_v2")


def _effective_cutoff_seconds(args: argparse.Namespace) -> int:
    # CLI override wins. Otherwise honour the same env var relay_v2 uses
    # so the migration matches the runtime's own idea of "stale".
    if args.older_than_minutes and args.older_than_minutes > 0:
        return int(args.older_than_minutes) * 60
    try:
        env_v = int(os.getenv("MINIAPP_STALE_PROCESSING_S", "900") or "900")
        return env_v if env_v > 0 else 900
    except (ValueError, TypeError):
        return 900


def main() -> int:
    log = _configure_logging()
    args = _parse_args()
    cutoff_s = _effective_cutoff_seconds(args)
    now = time.time()
    threshold = now - cutoff_s

    try:
        import db  # parent project
        import gallery_state as gs  # V2 module
    except Exception as e:  # noqa: BLE001
        log.error("cannot import parent project (db / gallery_state): %s", e)
        return 2

    try:
        conn = db.connect()
    except Exception as e:  # noqa: BLE001
        log.error("cannot connect to MongoDB: %s", e)
        return 2

    try:
        # Find every PROCESSING doc whose `started_at` (or created_at) is
        # older than the cutoff. The index we added in db._ensure_indexes
        # (partial: status=PROCESSING) makes this scan cheap.
        query = {
            "status": gs.STATUS_PROCESSING,
            "$or": [
                {"started_at":  {"$lt": threshold}},
                {"started_at":  {"$exists": False}, "created_at": {"$lt": threshold}},
            ],
        }
        cursor = conn.galleries.find(query, {"_id": 1, "url": 1, "started_at": 1, "created_at": 1})
        if args.limit and args.limit > 0:
            cursor = cursor.limit(int(args.limit))

        candidates: List[Dict[str, Any]] = list(cursor)
        log.info(
            "found %d stuck PROCESSING gallery docs older than %d minutes",
            len(candidates), cutoff_s // 60,
        )
        if not candidates:
            return 0

        # Show a small sample.
        for row in candidates[:5]:
            age = int(now - float(row.get("started_at") or row.get("created_at") or now))
            log.info("  candidate _id=%s  age=%ds  url=%s",
                     row.get("_id"), age, (row.get("url") or "")[:80])
        if len(candidates) > 5:
            log.info("  ... and %d more", len(candidates) - 5)

        if args.dry_run:
            log.info("--dry-run: no writes performed")
            return 0

        # Mutate: mark each as FAILED_RECOVERED with a clear reason.
        # We use a status name that's not in gs.TERMINAL_STATUSES so a
        # subsequent dedup_check treats it as a retryable tombstone the
        # same way FAILED_TIMEOUT is treated.
        updated = 0
        for row in candidates:
            gid = row["_id"]
            try:
                gs.mark_failed(
                    conn, str(gid),
                    status="FAILED_RECOVERED",
                    reason=(
                        f"migrate_v2_recover_stuck: PROCESSING for >{cutoff_s}s "
                        f"across restart; tombstoned so dedup gate allows retry."
                    ),
                    purge=False,
                )
                updated += 1
            except Exception as e:  # noqa: BLE001
                log.warning("failed to update _id=%s: %s", gid, e)
        log.info("marked %d/%d docs as FAILED_RECOVERED", updated, len(candidates))
        return 0
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    sys.exit(main())
