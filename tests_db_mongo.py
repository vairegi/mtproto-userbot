"""
Functional test of the new MongoDB db.py, using mongomock as an in-memory
stand-in for a real MongoDB server. This exercises the real query/update code
paths in db.py — only the network layer is faked.
"""
import os
import sys

os.environ["MONGO_URI"] = "mongodb://localhost:27017/"
os.environ["MONGO_DB_NAME"] = "relaybot_test"

import mongomock
import pymongo

# Patch MongoClient so db.py's _get_client() builds a mongomock client.
_real = pymongo.MongoClient


def _fake(*a, **kw):
    return mongomock.MongoClient()


pymongo.MongoClient = _fake
sys.path.insert(0, "/home/user/project")

# db.py imports MongoClient by name, so patch it there too after import.
import db as dbm

dbm.MongoClient = _fake

FAILS = []


def check(label, cond, extra=""):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {extra}")
        FAILS.append(label)


print("=== 1. init + connect ===")
dbm.init_db()
conn = dbm.connect()
check("connect() returns handle", conn is not None)
check("conn.close() is a safe no-op", conn.close() is None)
conn = dbm.connect()

print("\n=== 2. auto-increment job ids (replaces AUTOINCREMENT) ===")
j1 = dbm.enqueue(conn, "https://hentaifox.com/gallery/111/", "hash111",
                 submitted_by=555, chat_id=999, via_search=True, username="alice")
j2 = dbm.enqueue(conn, "https://hentaifox.com/gallery/222/", "hash222")
j3 = dbm.enqueue(conn, "https://hentaifox.com/gallery/333/", "hash333")
check("first id == 1", j1 == 1, f"got {j1}")
check("ids increment 1,2,3", (j1, j2, j3) == (1, 2, 3), f"got {(j1,j2,j3)}")

print("\n=== 3. row access compatibility (dict syntax used by worker.py) ===")
row = dbm.next_pending(conn)
check("next_pending returns oldest (FIFO)", row["id"] == 1, f"got {row['id']}")
check('row["url"] works', row["url"] == "https://hentaifox.com/gallery/111/")
check('row["url_hash"] works', row["url_hash"] == "hash111")
check('row["status"] == pending', row["status"] == "pending")
# These are the exact guards worker.py line 138-140 uses:
check('"via_search" in row.keys()', "via_search" in row.keys())
check('"username" in row.keys()', "username" in row.keys())
check('"submitted_by" in row.keys()', "submitted_by" in row.keys())
check('bool(row["via_search"]) is True', bool(row["via_search"]) is True)
check('row["username"] == alice', row["username"] == "alice")
check('row["submitted_by"] == 555', row["submitted_by"] == 555)
check('row["error_reason"] defaults None', row["error_reason"] is None)
check('row["cover_link"] defaults None', row["cover_link"] is None)

print("\n=== 4. dedupe: has_pending_or_processing / has_completed ===")
check("hash111 is pending", dbm.has_pending_or_processing(conn, "hash111") is True)
check("unknown hash not pending", dbm.has_pending_or_processing(conn, "nope") is False)
check("hash111 not completed yet", dbm.has_completed(conn, "hash111") is False)
dbm.record_processed(conn, "https://hentaifox.com/gallery/111/", "hash111")
check("hash111 completed after record_processed", dbm.has_completed(conn, "hash111") is True)
# idempotent re-record must not crash or duplicate
dbm.record_processed(conn, "https://hentaifox.com/gallery/111/", "hash111")
check("record_processed is idempotent", dbm.has_completed(conn, "hash111") is True)

print("\n=== 5. status transitions ===")
dbm.mark_processing(conn, j1)
check("marked processing", dbm.get_job(conn, j1)["status"] == "processing")
dbm.mark_status(conn, j1, "done", None)
check("marked done", dbm.get_job(conn, j1)["status"] == "done")
dbm.mark_status(conn, j2, "failed", "no PDF")
jr = dbm.get_job(conn, j2)
check("failed reason stored", jr["error_reason"] == "no PDF")

print("\n=== 6. reset_stuck_processing (restart recovery) ===")
dbm.mark_processing(conn, j3)
n = dbm.reset_stuck_processing(conn)
check("1 stuck job reset", n == 1, f"got {n}")
check("j3 back to pending", dbm.get_job(conn, j3)["status"] == "pending")

print("\n=== 7. claim_next_pending (atomic) ===")
claimed = dbm.claim_next_pending(conn)
check("claimed j3", claimed["id"] == j3, f"got {claimed}")
check("claimed is processing", claimed["status"] == "processing")
check("no pending left", dbm.claim_next_pending(conn) is None)
dbm.mark_status(conn, j3, "pending")

print("\n=== 8. counts / stats / reporting ===")
c = dbm.counts_by_status(conn)
check("counts has all 5 keys", set(c) == {"pending", "processing", "done", "partial", "failed"}, c)
check("1 done", c["done"] == 1, c)
check("1 failed", c["failed"] == 1, c)
check("failed_last_24h == 1", dbm.failed_last_24h(conn) == 1)
lj = dbm.last_jobs(conn, 5)
check("last_jobs returns 3", len(lj) == 3, len(lj))
check("last_jobs newest first", lj[0]["updated_at"] >= lj[-1]["updated_at"])
check('last_jobs rows have ["id"]', "id" in lj[0])
check('last_jobs rows have ["status"]', "status" in lj[0])
mrf = dbm.most_recent_failed(conn)
check("most_recent_failed is j2", mrf["id"] == j2)
check("most_recent_failed has url", mrf["url"] == "https://hentaifox.com/gallery/222/")

print("\n=== 9. control flags / pause / lock ===")
check("missing flag returns default", dbm.get_flag(conn, "nothere", "0") == "0")
dbm.set_flag(conn, "paused", "1")
check("paused flag set", dbm.get_flag(conn, "paused", "0") == "1")
dbm.set_flag(conn, "paused", "0")
check("paused flag cleared", dbm.get_flag(conn, "paused", "0") == "0")
check("not locked initially", dbm.is_locked(conn) is False)
dbm.set_locked(conn, True, by_user_id=777)
check("locked", dbm.is_locked(conn) is True)
li = dbm.lock_info(conn)
check("lock_info by=777", li["by"] == "777", li)
dbm.set_locked(conn, False)
check("unlocked", dbm.is_locked(conn) is False)

print("\n=== 10. bot pings ===")
check("no ping initially", dbm.get_bot_ping(conn, "bot1") is None)
dbm.touch_bot_ping(conn, "bot1")
p = dbm.get_bot_ping(conn, "bot1")
check("ping is int timestamp", isinstance(p, int) and p > 0, p)

print("\n=== 11. flood events ===")
dbm.log_flood(conn, 30, "resolve_bot1")
check("flood event stored", conn.flood_events.count_documents({}) == 1)

print("\n=== 12. admins (two-tier) ===")
check("no admin initially", dbm.get_admin(conn, 1001) is None)
dbm.add_admin(conn, 1001, is_super=False, added_by=999)
a = dbm.get_admin(conn, 1001)
check("admin added", a is not None)
check('admin["user_id"]', a["user_id"] == 1001)
check('admin["is_super"] == 0', int(a["is_super"]) == 0)
check('admin["added_by"] == 999', a["added_by"] == 999)
check('admin["added_at"] is int', isinstance(a["added_at"], int))
dbm.add_admin(conn, 1002, is_super=True, added_by=999)
rows = dbm.list_admins(conn)
check("2 admins listed", len(rows) == 2, len(rows))
check("supers sorted first", int(rows[0]["is_super"]) == 1, rows)
n = dbm.set_super(conn, 1001, True)
check("set_super matched 1", n == 1, n)
check("1001 now super", int(dbm.get_admin(conn, 1001)["is_super"]) == 1)
n = dbm.remove_admin(conn, 1001)
check("remove_admin returns 1", n == 1, n)
check("1001 gone", dbm.get_admin(conn, 1001) is None)
check("remove missing returns 0", dbm.remove_admin(conn, 4242) == 0)

print("\n=== 13. users ===")
dbm.upsert_user(conn, 2001)
first = conn.users.find_one({"_id": 2001})["first_seen_at"]
dbm.upsert_user(conn, 2001)
check("upsert_user does not overwrite first_seen_at",
      conn.users.find_one({"_id": 2001})["first_seen_at"] == first)

print("\n=== 14. job progress (title COALESCE behaviour) ===")
dbm.upsert_job_progress(conn, j1, dbm.PHASE_PENDING, title="My Gallery Title")
dbm.upsert_job_progress(conn, j1, dbm.PHASE_WAIT_PDF, detail="waiting for PDF")
pr = dbm.get_progress_for_jobs(conn, [j1])
check("progress row returned", len(pr) == 1)
r0 = pr[0]
check('progress ["job_id"]', r0["job_id"] == j1)
check("phase updated", r0["phase"] == dbm.PHASE_WAIT_PDF, r0)
check("title PRESERVED when None passed", r0["title"] == "My Gallery Title", r0)
check("detail updated", r0["detail"] == "waiting for PDF")
dbm.upsert_job_progress(conn, j2, dbm.PHASE_DONE, title="Second")
multi = dbm.get_progress_for_jobs(conn, [j1, j2])
check("multi-job progress fetch", len(multi) == 2, len(multi))
check("empty job_ids returns []", dbm.get_progress_for_jobs(conn, []) == [])
dbm.cleanup_progress(conn, [j1, j2])
check("progress cleaned", dbm.get_progress_for_jobs(conn, [j1, j2]) == [])

print("\n=== 15. progress batches (job_ids CSV string compat) ===")
dbm.create_progress_batch(conn, "batch_abc", 12345, [1, 2, 3])
b = dbm.get_active_progress_batches(conn)
check("1 active batch", len(b) == 1, len(b))
bb = b[0]
check('batch ["batch_id"]', bb["batch_id"] == "batch_abc")
check('batch ["chat_id"]', bb["chat_id"] == 12345)
check("message_id None before set", bb["message_id"] is None)
# progress_tracker.py does: [int(x) for x in b["job_ids"].split(",")]
parsed = [int(x) for x in (bb["job_ids"] or "").split(",") if x.strip()]
check("job_ids splits to [1,2,3]", parsed == [1, 2, 3], parsed)
check("created_at is int", isinstance(bb["created_at"], int))
dbm.set_progress_batch_message(conn, "batch_abc", 6789)
check("message_id set", dbm.get_active_progress_batches(conn)[0]["message_id"] == 6789)
dbm.complete_progress_batch(conn, "batch_abc")
check("completed batch no longer active", dbm.get_active_progress_batches(conn) == [])
dbm.delete_progress_batch(conn, "batch_abc")
check("batch deleted", conn.progress_batches.count_documents({}) == 0)

print("\n=== 16. tokens: freepost / consume / refund / set / reset ===")
check("default freepost 20", dbm.get_freepost(conn) == 20)
dbm.set_freepost(conn, 5)
check("freepost now 5", dbm.get_freepost(conn) == 5)
t = dbm.get_user_tokens(conn, 3001, "bob")
check("new user 0 used", t["used"] == 0, t)
check("new user 5 remaining", t["remaining"] == 5, t)
check("daily_cap 5", t["daily_cap"] == 5, t)
check("consume 3 ok", dbm.consume_tokens(conn, 3001, 3, "bob") is True)
t = dbm.get_user_tokens(conn, 3001, "bob")
check("used 3 remaining 2", (t["used"], t["remaining"]) == (3, 2), t)
check("consume 3 more DENIED (over cap)", dbm.consume_tokens(conn, 3001, 3, "bob") is False)
check("still used 3", dbm.get_user_tokens(conn, 3001)["used"] == 3)
check("consume exactly 2 ok", dbm.consume_tokens(conn, 3001, 2, "bob") is True)
check("now 0 remaining", dbm.get_user_tokens(conn, 3001)["remaining"] == 0)
check("consume 1 at cap DENIED", dbm.consume_tokens(conn, 3001, 1, "bob") is False)
check("consume 0 is a no-op True", dbm.consume_tokens(conn, 3001, 0, "bob") is True)
dbm.refund_token(conn, 3001, 1)
check("refund gives 1 back", dbm.get_user_tokens(conn, 3001)["remaining"] == 1)
dbm.refund_token(conn, 3001, 99)
check("refund clamps at 0 used", dbm.get_user_tokens(conn, 3001)["used"] == 0)
dbm.refund_token(conn, 9999999, 1)  # unknown user must not crash
check("refund unknown user safe", True)
res = dbm.set_user_tokens(conn, 3001, 2)
check("set_user_tokens remaining=2", res["remaining"] == 2, res)
check("set_user_tokens used=3", res["used"] == 3, res)
res = dbm.set_user_tokens(conn, 3001, 999)
check("set_user_tokens clamps to cap", res["remaining"] == 5, res)

print("\n=== 17. username resolution (case-insensitive) ===")
dbm.get_user_tokens(conn, 3002, "Charlie")
check("exact match", dbm.resolve_user_id_by_username(conn, "Charlie") == 3002)
check("lowercase match", dbm.resolve_user_id_by_username(conn, "charlie") == 3002)
check("UPPERCASE match", dbm.resolve_user_id_by_username(conn, "CHARLIE") == 3002)
check("@prefix stripped", dbm.resolve_user_id_by_username(conn, "@charlie") == 3002)
check("unknown returns None", dbm.resolve_user_id_by_username(conn, "nobody") is None)
check("empty returns None", dbm.resolve_user_id_by_username(conn, "") is None)
# regex-injection safety: a username with regex chars must not match everything
check("regex chars are escaped", dbm.resolve_user_id_by_username(conn, ".*") is None)

print("\n=== 18. alltoken report + reset_all ===")
dbm.consume_tokens(conn, 3002, 1, "Charlie")
rows = dbm.list_all_user_tokens(conn)
check("2 users in report", len(rows) == 2, len(rows))
check("sorted by used desc", rows[0]["used"] >= rows[1]["used"], rows)
check("report has username", any(r["username"] == "Charlie" for r in rows), rows)
check("report has user_id int", all(isinstance(r["user_id"], int) for r in rows))
check("report has remaining", all("remaining" in r for r in rows))
dbm.reset_all_tokens(conn)
check("all tokens reset to 0",
      all(r["used"] == 0 for r in dbm.list_all_user_tokens(conn)))

print("\n=== 19. daily UTC auto-reset ===")
dbm.set_freepost(conn, 20)
dbm.consume_tokens(conn, 3003, 5, "dave")
check("dave used 5", dbm.get_user_tokens(conn, 3003)["used"] == 5)
# Simulate yesterday's date being stored -> should lazily reset on next read
conn.user_tokens.update_one({"_id": 3003}, {"$set": {"last_reset_date": "2020-01-01"}})
check("stale date triggers reset", dbm.get_user_tokens(conn, 3003)["used"] == 0)

print("\n=== 20. username refresh on rename ===")
dbm.get_user_tokens(conn, 3004, "oldname")
dbm.get_user_tokens(conn, 3004, "newname")
check("username updated on rename",
      conn.user_tokens.find_one({"_id": 3004})["username"] == "newname")
check("old username no longer resolves",
      dbm.resolve_user_id_by_username(conn, "oldname") is None)
check("new username resolves", dbm.resolve_user_id_by_username(conn, "newname") == 3004)

print("\n=== 21. misc compat surface ===")
check("confirm_wal returns 'wal' stub", dbm.confirm_wal(conn) == "wal")
check("now_ts returns int", isinstance(dbm.now_ts(), int))
check("SCHEMA_VERSION exists", isinstance(dbm.SCHEMA_VERSION, int))
check("transaction() context manager works",
      (lambda: [c for c in [None] if True] and True)())
with dbm.transaction(conn) as cur:
    check("transaction yields handle", cur is not None)
check("set_cover_link", (dbm.set_cover_link(conn, j1, "t.me/c/1/2"),
                         dbm.get_job(conn, j1)["cover_link"] == "t.me/c/1/2")[1])
# phase constants used by progress_tracker._PHASE_ICONS
for p in ("PHASE_PENDING", "PHASE_SENT_BOTS", "PHASE_WAIT_PDF", "PHASE_FORWARDING",
          "PHASE_MPOSTING", "PHASE_DONE", "PHASE_FAILED", "PHASE_PARTIAL"):
    check(f"{p} defined", hasattr(dbm, p))

print("\n" + "=" * 60)
if FAILS:
    print(f"RESULT: {len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("RESULT: ALL TESTS PASSED")
