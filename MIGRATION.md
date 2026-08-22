# Database Separation — MongoDB ⇄ Turso split (v0.39)

## Goal

* **MongoDB** = durable user/state only. Profiles, bookmarks rows, ratings,
  shares, queue (pending rows), processed_urls, admins, users, flags,
  bookmarks-history.
* **Turso** = every cache layer. Search pages, gallery metadata, tag cache,
  bookmark cover bytes (deduped), shared token-bucket replicas, future
  Bot 2 queue/done.

## New env vars (opt-in toggles, default OFF = behaviour is identical to pre-refactor)

| Name                          | Service | Default | Rollback to enable legacy dual-write |
|-------------------------------|---------|---------|--------------------------------------|
| `BOT0_NH_MONGO_WRITES`        | Bot 0   | `0`     | `1` |
| `BOT1_CACHE_MONGO_MIRROR`     | Bot 1   | `0`     | `1` |
| `MONGO_CLEANUP_CONFIRM`       | admin   | unset   | `yes-do-it` to permit destructive run |

Nothing in the existing env schema is removed. Toggling the two flags back
to `1` re-enables the legacy Mongo mirror writes instantly (the read paths
still check both stores).

## Files touched in this bundle

```
refactor_bundle/
├── ScraperBot/
│   ├── main.py                                   # boot calls turso_schema.ensure_schema()
│   ├── app/cache.py                              # BOT1_CACHE_MONGO_MIRROR gate
│   ├── Procfile / requirements.txt               # unchanged
│   └── app/services/turso_schema.py              # NEW — idempotent SQL
├── miniapp/
│   └── backend/
│       ├── main.py                               # startup hook: ensure_turso_schema
│       ├── app/services/nhentai_cache.py         # BOT0_NH_MONGO_WRITES gate,
│       │                                           bm_cover_get/put helpers
│       └── scripts/mongo_cache_cleanup.py        # destructive-safe dry-run
└── MIGRATION.md                                  # this file
```

## Migration order (you must follow)

1. **Deploy Bot 0 first**, leave `BOT0_NH_MONGO_WRITES=0`. Watch logs for
   `[TURSO CACHE HIT]` / `[CACHE MISS]` lines. `nhentai_cache` collection
   should stop growing.
2. **Deploy Bot 1**, leave `BOT1_CACHE_MONGO_MIRROR=0`. Watch logs for
   `📝 WRITE key=... turso=True mongo=False`.
3. **Schema migration auto-runs on every boot.** Manual fallback:
   `python3 -m ScraperBot.app.services.turso_schema`.
4. After ≥ 7 days of stable logs, drain Mongo:
   ```bash
   # DRY-RUN first — it lists everything it would touch
   MONGO_URI="$MONGODB_URI"      python3 miniapp/backend/scripts/mongo_cache_cleanup.py --drop --prune
   # Destructive run — requires both --yes and env gate
   MONGO_CLEANUP_CONFIRM=yes-do-it      python3 miniapp/backend/scripts/mongo_cache_cleanup.py --drop --prune --yes
   ```

## Rollback

Set `BOT0_NH_MONGO_WRITES=1` and `BOT1_CACHE_MONGO_MIRROR=1` in the
respective Render services. Restart both. The legacy Mongo mirrors are
still populated by the gate-enabled writes; the read path checks them
first if present and re-warms Monment, then continues serving from Turso.

## What is NOT in the bundle

* No secrets, no Mongo URI, no Turso token. Use Render Secret Files /
  Parameter Store.
* No Atlas IP allow-list edits — done via console.
* No Turso replica in `ap-singapore` — that is provisioned separately.

## Sanity-check commands

```bash
# Mongo collection sizes AFTER refactor
python3 -c "
import pymongo, os
db = pymongo.MongoClient(os.environ['MONGO_URI'],
    serverSelectionTimeoutMS=4000)[os.environ.get('MONGO_DB_NAME','relaybot')]
for c in sorted(db.list_collection_names()):
    print(f'{c:30s} docs={db[c].estimated_document_count():>8}  size={db.command("collStats", c).get("size", 0):>10}')
"
```

After cleanup, `nhentai_cache` size should be 0. `progress_events`,
`job_progress`, `bot2_latency` should fall to 0 docs (any new live rows
auto-expire via TTL).
