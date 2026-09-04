"""
purge_legacy_search_keys.py — v12.55 one-off migration. RESUMABLE.

Deletes legacy `search:search:<q>:p<N>` rows from Turso nhentai_cache
(2,136 rows at census time, 2026-09-04). These rows are read-probed on
every typed search but are superseded by canonical `search:q=…|sort=…`
rows; after this purge the probes (and the 2,136 dead rows) are gone.

RESUMABLE: each batch re-SELECTs the *remaining* legacy keys, so if the
run dies (Ctrl+C, connection drop), re-running the SAME command simply
continues with what's left. No state file needed.

STEP-BY-STEP (Windows cmd):
  1.  set TURSO_DATABASE_URL=https://doujinshi-cache-vairegi.aws-us-west-2.turso.io
  2.  set TURSO_AUTH_TOKEN=<your token>
  3.  python scripts/purge_legacy_search_keys.py --dry-run
        -> prints the row count, deletes NOTHING. Expect ~2,136.
  4.  python scripts/purge_legacy_search_keys.py
        -> batched deletes, progress line each batch.
  5.  If it stops early, just run step 4 again — it resumes.

Uses only the stdlib + the same HTTP /v2/pipeline endpoint the bots use.
"""
import json
import os
import sys
import time
import urllib.request

BASE = os.environ["TURSO_DATABASE_URL"].rstrip("/") + "/v2/pipeline"
TOK = os.environ["TURSO_AUTH_TOKEN"]
DRY = "--dry-run" in sys.argv
BATCH = 150          # keys per DELETE statement (SQLite var limit is 999)
SLEEP_SEC = 0.4      # gentle pacing between batches


def pipeline(sql, args=None):
    body = {"requests": [
        {"type": "execute", "stmt": {"sql": sql, "args": [
            {"type": "text", "value": str(a)} for a in (args or [])]}},
        {"type": "close"}]}
    req = urllib.request.Request(
        BASE, data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOK}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    res = data["results"][0]
    if res.get("type") != "ok":
        raise RuntimeError(str(res.get("error"))[:300])
    result = res["response"]["result"]
    rows = [[c.get("value") for c in row] for row in result.get("rows", [])]
    return rows, result.get("affected_row_count", 0)


def main():
    rows, _ = pipeline(
        'SELECT COUNT(*) FROM nhentai_cache WHERE "key" LIKE \'search:search:%\'')
    total = int(rows[0][0])
    print(f"[purge] legacy search:search:* rows found: {total}")
    if DRY:
        print("[purge] DRY RUN — nothing deleted. Re-run without --dry-run.")
        return
    if total == 0:
        print("[purge] nothing to do.")
        return

    deleted = 0
    while True:
        # cursor = remaining keys themselves -> resumable by construction
        batch, _ = pipeline(
            'SELECT "key" FROM nhentai_cache WHERE "key" LIKE '
            '\'search:search:%\' ORDER BY "key" LIMIT ?', (BATCH,))
        if not batch:
            break
        keys = [r[0] for r in batch]
        ph = ",".join("?" for _ in keys)
        pipeline(f'DELETE FROM nhentai_cache WHERE "key" IN ({ph})', keys)
        deleted += len(keys)
        print(f"[purge] deleted {deleted}/{total} "
              f"(last: {keys[-1][:60]})", flush=True)
        time.sleep(SLEEP_SEC)
    print(f"[purge] DONE. Deleted {deleted} legacy rows.")


if __name__ == "__main__":
    main()
