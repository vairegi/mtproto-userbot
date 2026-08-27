#!/usr/bin/env python3
"""backfill_backup_channel.py — one-time BackupDB backfill (v1.22).

Run ONCE on your own PC (Windows 11 CMD / PowerShell). It:

  1. Connects to Telegram as your userbot session.
  2. Resolves (or AUTO-CREATES) the Backup Database Channel and stores
     its id in Mongo ``backup_state`` (doc _id="state").
  3. Queries Mongo ``galleries`` for every doc that has
     ``db_cover_msg_id``/``db_pdf_msg_id`` (i.e. Mini-App-era posts ONLY —
     the old text messages and 2,000+ legacy posts in the channel are
     never touched, because they have no Mongo doc).
  4. For each gallery: verifies the messages in the Main channel still
     exist AND still match the gid (cover: photo or gid in caption;
     PDF: application/pdf document or gid in filename) — so a deleted /
     swapped message is NEVER forwarded as the wrong file.
  5. Server-side forwards cover+PDF into BackupDB and stamps the new
     BackupDB message ids back onto the SAME galleries doc.
  6. Works in SMALL BATCHES (default 25 galleries, then a pause) so your
     laptop/CMD never gets stuck, and is fully RESUMABLE — any doc that
     already has ``backup_cover_msg_id`` is skipped. If it crashes, your
     laptop sleeps, or FloodWait hits: just run the same command again.

Required env vars (set them in CMD before running — see GUIDE_APPEND.txt):
    API_ID, API_HASH               — from https://my.telegram.org
    STRING_SESSION                 — your Telethon string session
    MONGO_URI                      — Mongo connection string
    MONGO_DB_NAME                  — default "relaybot"
    DATABASE_CHANNEL_ID            — the Main DB channel id
Optional:
    BACKUP_DB_CHANNEL_ID           — pin an existing backup channel
    BACKFILL_BATCH_SIZE            — galleries per batch (default 25)
    BACKFILL_BATCH_SLEEP           — seconds between batches (default 30)
    BACKFILL_MSG_SLEEP             — seconds between galleries (default 1.2)

Flags:
    --dry-run    verify + report only, forward nothing, write nothing
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from pymongo import MongoClient

STATE_ID = "state"


def _req(name: str) -> str:
    v = (os.environ.get(name) or "").strip()
    if not v:
        sys.exit(f"FATAL: env var {name} is not set. See GUIDE_APPEND.txt.")
    return v


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------

async def resolve_or_create_backup_channel(client, mongo_db) -> int:
    """Return the BackupDB channel id (full -100... form)."""
    env_id = _int_env("BACKUP_DB_CHANNEL_ID", 0)
    if env_id:
        print(f"[setup] using BACKUP_DB_CHANNEL_ID from env: {env_id}")
        mongo_db.backup_state.update_one(
            {"_id": STATE_ID},
            {"$set": {"backup_channel_id": env_id}}, upsert=True)
        return env_id

    state = mongo_db.backup_state.find_one({"_id": STATE_ID}) or {}
    if state.get("backup_channel_id"):
        ch = int(state["backup_channel_id"])
        print(f"[setup] reusing BackupDB channel from Mongo state: {ch}")
        return ch

    print("[setup] creating BackupDB channel via userbot ...")
    r = await client(functions.channels.CreateChannelRequest(
        title="DU Database Backup",
        about="High-availability mirror of the Main Database Channel "
              "(auto-created by backfill_backup_channel.py v1.22).",
        broadcast=True,
    ))
    chat = r.chats[0]
    full_id = int(f"-100{chat.id}")
    mongo_db.backup_state.update_one(
        {"_id": STATE_ID},
        {"$set": {"backup_channel_id": full_id,
                  "backup_channel_created_at": time.time()}},
        upsert=True)
    print(f"[setup] created BackupDB channel id={full_id}")
    print("[setup] TIP: also pin this as BACKUP_DB_CHANNEL_ID on Render.")
    return full_id


def _msg_matches_cover(msg, gid: str) -> bool:
    """Cover must be a photo, or its caption must carry the gid."""
    if msg is None:
        return False
    if getattr(msg, "photo", None) is not None:
        return True
    text = (getattr(msg, "raw_text", "") or "")
    return str(gid) in text


def _msg_matches_pdf(msg, gid: str) -> bool:
    """PDF must be an application/pdf document, or carry gid in filename."""
    if msg is None:
        return False
    doc = getattr(msg, "document", None)
    if doc is None:
        return False
    if (getattr(doc, "mime_type", "") or "") == "application/pdf":
        return True
    for attr in getattr(doc, "attributes", []) or []:
        name = getattr(attr, "file_name", "") or ""
        if str(gid) in name:
            return True
    return False


async def _get_with_flood(client, entity, ids):
    while True:
        try:
            return await client.get_messages(entity, ids=ids)
        except FloodWaitError as fw:
            print(f"  [flood] get_messages waits {fw.seconds}s ...")
            await asyncio.sleep(fw.seconds + 2)


async def _forward_with_flood(client, dest, msg_ids, src):
    while True:
        try:
            return await client.forward_messages(
                dest, msg_ids, src, drop_author=True)
        except FloodWaitError as fw:
            print(f"  [flood] forward waits {fw.seconds}s ...")
            await asyncio.sleep(fw.seconds + 2)


# ---------------------------------------------------------------------------
# Main backfill loop
# ---------------------------------------------------------------------------

async def run(dry_run: bool) -> None:
    api_id = int(_req("API_ID"))
    api_hash = _req("API_HASH")
    session = _req("STRING_SESSION")
    main_ch = int(_req("DATABASE_CHANNEL_ID"))

    mongo = MongoClient(_req("MONGO_URI"))
    mdb = mongo[(os.environ.get("MONGO_DB_NAME") or "relaybot").strip()]
    galleries = mdb["galleries"]

    batch_size = max(1, _int_env("BACKFILL_BATCH_SIZE", 25))
    batch_sleep = max(0, _int_env("BACKFILL_BATCH_SLEEP", 30))
    msg_sleep = max(0.0, float(os.environ.get("BACKFILL_MSG_SLEEP") or 1.2))

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("FATAL: STRING_SESSION is not authorized. Regenerate it.")

    backup_ch = await resolve_or_create_backup_channel(client, mdb)
    main_entity = await client.get_entity(main_ch)
    backup_entity = await client.get_entity(backup_ch)
    print(f"[setup] main={main_ch}  backup={backup_ch}")

    # Only Mini-App-era docs: they are the ONLY ones with db_* msg ids.
    # Resume safety: anything already stamped with backup_cover_msg_id
    # (or a terminal status) is skipped, so re-running is always safe.
    query = {
        "db_cover_msg_id": {"$exists": True, "$ne": None},
        "backup_cover_msg_id": {"$exists": False},
        "backup_status": {"$nin": ["missing_in_main", "needs_review"]},
    }
    total = galleries.count_documents(query)
    print(f"[plan] {total} galleries left to back up "
          f"(batch={batch_size}, sleep={msg_sleep}s/item, "
          f"{batch_sleep}s/batch, dry_run={dry_run})")

    ok = cover_only = missing = review = failed = done = 0
    review_log = open("backfill_review.log", "a", encoding="utf-8")

    cursor = galleries.find(
        query,
        {"db_cover_msg_id": 1, "db_pdf_msg_id": 1, "title": 1},
    ).sort("_id", 1)

    for doc in cursor:
        gid = str(doc["_id"])
        cover_id = doc.get("db_cover_msg_id")
        pdf_id = doc.get("db_pdf_msg_id")
        done += 1

        try:
            ids = [int(m) for m in (cover_id, pdf_id) if m]
            msgs = await _get_with_flood(client, main_entity, ids)
            msgs = msgs if isinstance(msgs, list) else [msgs]
            by_id = {int(getattr(m, "id", 0) or 0): m
                     for m in msgs if m is not None}
            cover_msg = by_id.get(int(cover_id)) if cover_id else None
            pdf_msg = by_id.get(int(pdf_id)) if pdf_id else None

            # ---- integrity checks (never forward the wrong file) ----
            if cover_msg is None and (pdf_id and pdf_msg is None):
                missing += 1
                status = "missing_in_main"
                print(f"[{done}/{total}] {gid}: BOTH gone from Main — "
                      f"marked missing_in_main")
                if not dry_run:
                    galleries.update_one(
                        {"_id": gid},
                        {"$set": {"backup_status": status,
                                  "backed_up_at": time.time()}})
                continue
            if cover_msg is not None and not _msg_matches_cover(cover_msg, gid):
                review += 1
                print(f"[{done}/{total}] {gid}: cover mismatch — needs_review")
                review_log.write(f"{gid}\tcover\t{cover_id}\n")
                if not dry_run:
                    galleries.update_one(
                        {"_id": gid},
                        {"$set": {"backup_status": "needs_review",
                                  "backed_up_at": time.time()}})
                continue
            if pdf_id and pdf_msg is not None and not _msg_matches_pdf(pdf_msg, gid):
                review += 1
                print(f"[{done}/{total}] {gid}: pdf mismatch — needs_review")
                review_log.write(f"{gid}\tpdf\t{pdf_id}\n")
                if not dry_run:
                    galleries.update_one(
                        {"_id": gid},
                        {"$set": {"backup_status": "needs_review",
                                  "backed_up_at": time.time()}})
                continue

            fwd_ids = [m for m in (int(cover_id) if cover_msg else None,
                                   int(pdf_id) if pdf_msg else None) if m]
            if dry_run:
                print(f"[{done}/{total}] {gid}: OK — would forward {fwd_ids}")
                ok += 1
            else:
                res = await _forward_with_flood(
                    client, backup_entity, fwd_ids, main_entity)
                rmsgs = res if isinstance(res, list) else [res]
                bc = bp = None
                if cover_msg and len(rmsgs) > 0 and rmsgs[0] is not None:
                    bc = int(getattr(rmsgs[0], "id", 0) or 0) or None
                if pdf_msg and len(rmsgs) > (1 if cover_msg else 0):
                    last = rmsgs[-1]
                    bp = int(getattr(last, "id", 0) or 0) or None
                if bc or bp:
                    st = "ok" if (bp or not pdf_id) else "cover_only"
                    upd = {"backup_channel_id": int(backup_ch),
                           "backup_status": st,
                           "backed_up_at": time.time()}
                    if bc:
                        upd["backup_cover_msg_id"] = bc
                    if bp:
                        upd["backup_pdf_msg_id"] = bp
                    galleries.update_one({"_id": gid}, {"$set": upd})
                    if st == "ok":
                        ok += 1
                    else:
                        cover_only += 1
                    print(f"[{done}/{total}] {gid}: backed up "
                          f"cover={bc} pdf={bp} ({st})")
                else:
                    failed += 1
                    print(f"[{done}/{total}] {gid}: forward gave no ids — "
                          f"will retry on next run")

            if msg_sleep:
                await asyncio.sleep(msg_sleep)
            if done % batch_size == 0:
                print(f"--- batch pause {batch_sleep}s "
                      f"(ok={ok} cover_only={cover_only} missing={missing} "
                      f"review={review} failed={failed}) ---")
                await asyncio.sleep(batch_sleep)

        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"[{done}/{total}] {gid}: ERROR {e!r} — continuing")

    review_log.close()
    print("=" * 60)
    print(f"DONE. ok={ok} cover_only={cover_only} missing_in_main={missing} "
          f"needs_review={review} failed={failed}")
    if review:
        print("See backfill_review.log for the mismatch list.")
    print("Re-run the same command any time — already-backed-up galleries "
          "are skipped automatically.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    asyncio.run(run(dry_run=args.dry_run))
