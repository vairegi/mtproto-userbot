#!/usr/bin/env python3
"""mongo_cache_cleanup.py — v0.39 destructive-safe Mongo cleanup.

Reads MONGO_URI / MONGO_DB_NAME from env. Defaults to DRY-RUN. Two-step
gate required for destructive mode:
  1) pass --yes AND --{drop,prune} on argv
  2) set MONGO_CLEANUP_CONFIRM=yes-do-it in env

Collections DROPPED (mirror of Turso):
  * nhentai_cache

Collections PRUNED (TTL-tagged live rows):
  * progress_events     (> 24 h)
  * job_progress        (> 24 h)
  * progress_batches    (finalized > 7 d)
  * bot2_latency        (> 14 d)

NOT TOUCHED (durable user/state):
  * galleries, queue (pending rows), processed_urls, admins, users,
    user_tokens, counters, miniapp_users, miniapp_bookmarks (rows),
    miniapp_ratings, miniapp_shares, miniapp_usage,
    miniapp_improvements, miniapp_scheduled_deletes,
    miniapp_scheduled_delete_runs, miniapp_broadcast_runs,
    miniapp_settings, miniapp_pending_deliveries,
    miniapp_pending_join_requests, popup_views, flood_events,
    bot_pings, control_flags, state, bucket
"""
from __future__ import annotations
import argparse, logging, os, sys, time
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mongo_cleanup")

DROP_COLLECTIONS = ("nhentai_cache",)
PRUNE = (
    ("progress_events",   24*3600, "ts"),
    ("job_progress",      24*3600, "updated_at"),
    ("progress_batches",   7*24*3600, "finalized_at"),
    ("bot2_latency",      14*24*3600, "ts"),
)


def _require_confirm(require_yes: bool) -> None:
    if require_yes and os.environ.get("MONGO_CLEANUP_CONFIRM") != "yes-do-it":
        sys.exit("ABORT: also set MONGO_CLEANUP_CONFIRM=yes-do-it in env")


def _open():
    import pymongo
    uri = os.environ["MONGO_URI"]
    dbn = os.environ.get("MONGO_DB_NAME", "relaybot")
    log.info("Connecting to MONGO_DB_NAME=%r (uri redacted)", dbn)
    return pymongo.MongoClient(uri, serverSelectionTimeoutMS=5000)[dbn]


def dry_run(db) -> int:
    log.info("DRY-RUN — listing planned operations")
    for name in DROP_COLLECTIONS:
        if name in db.list_collection_names():
            log.info("  DROP   %-22s docs=%d", name, db[name].estimated_document_count())
        else:
            log.info("  DROP   %-22s (not present, skip)", name)
    now = time.time()
    for name, ttl, field in PRUNE:
        if name in db.list_collection_names():
            log.info("  PRUNE  %-22s field=%s older_than=%ds", name, field, ttl)
        else:
            log.info("  PRUNE  %-22s (not present, skip)", name)
    return 0


def apply(db) -> int:
    rc = 0
    for name in DROP_COLLECTIONS:
        if name not in db.list_collection_names():
            continue
        log.info("DROP   %s", name)
        db.drop_collection(name)
        rc += 1
    now = time.time()
    for name, ttl, field in PRUNE:
        if name not in db.list_collection_names():
            continue
        coll = db[name]
        r = coll.delete_many({field: {"$lt": now - ttl}})
        log.info("PRUNE  %s  field=%s older=%ds deleted=%d",
                 name, field, ttl, r.deleted_count)
    return rc


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--drop", action="store_true")
    p.add_argument("--prune", action="store_true")
    args = p.parse_args()
    if not (args.drop or args.prune):
        p.print_help()
        return 0
    db = _open()
    if not args.yes:
        log.warning("No --yes: dry-run only")
        return dry_run(db)
    if not (args.drop or args.prune):
        p.error("Need --drop or --prune")
    _require_confirm(True)
    log.info("DESTRUCTIVE @ %s", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    n = apply(db)
    log.info("done — drop collections+%d, prune=N/A (handled above)", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
