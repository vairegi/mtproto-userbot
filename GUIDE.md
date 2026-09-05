# DoujinshiUniverse — Setup & Deploy Guide

## Repo layout
- Repo root = **BOT 0** (DoujinshiUniverse user-facing service): mini-app backend (FastAPI) + worker + admin bot.
- `ScraperBot/` = **BOT 1** (cache warmer; no user surface; deployed separately, Render Root Directory must be `ScraperBot`).
- BOT 2 = external `@Gallery_DLBot` (not in this repo).

## BOT 0 deploy (Render)
- Start command: `bash start.sh`
- Boot order: env check → Mongo check → one-shot Telethon session check (userbot.py, then exits) → supervised `admin_bot.py` + `worker.py` → uvicorn foreground on `$PORT`.
- 3 resident processes (v12.31+): uvicorn (foreground), admin_bot, worker. relay.py (legacy V1) was removed.
- Env required: API_ID, API_HASH, BOT_TOKEN (or ADMIN_BOT_TOKEN), MONGO_URI, STRING_SESSION, ADMIN_USER_ID, BOT2_USERNAME, DATABASE_CHANNEL_ID, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN.
- Monitors/health checks must hit `/healthz` (HEAD `/` returns 405).

## BOT 1 deploy (Render)
- New Web Service, same repo, Root Directory = `ScraperBot`, region as needed.
- Env: MONGO_URI, MONGO_DB_NAME, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, BOT1_TOKEN, BOT1_LOG_CHANNEL_ID, BOT1_ADMIN_KEY (+ pacing knobs; `BOT1_REGION=ap-singapore` on the Singapore service for the region-split bucket).

## Cache contract (BOT 0 reads these keys exactly)
- `search:<sort>:page<N>` (chip pages), `search:q=<query>|sort=<s>|page=<N>` (typed/tag, query lowercase+collapsed), `gallery:<id>` (detail).
- `search:*` payload must be a LIST of card dicts; `gallery:*` must be a normalized dict with `id`, `title`, `tag_groups`, `page1_url`. Any other shape reads as MISS.

## Housekeeping rules
- All docs live in this ONE file. Do not add new per-version .md files.
- relay.py / main.py at root were deleted (v12.32) — do not restore; relay_v2.py and ScraperBot/main.py are the live ones.
- To revert, `git checkout <commit>^ -- <path>` or redeploy the previous ZIP.

## v12.33 — multi-userbot pool (2026-08-20)

### What changed
- **`userbot_pool.py` (NEW).** `UserbotPool` owns N `telethon.TelegramClient` instances inside `worker.py`'s single asyncio loop. Least-in-flight dispatch over non-cooling slots. Zero extra resident processes — the 512 MB ceiling stays intact.
- **`worker.py`.** Boots a pool instead of a single client; each job runs under `async with pool.acquire() as slot`. When every slot is cooling, the job is re-queued and the loop sleeps briefly. On shutdown the whole pool is stopped.
- **`relay_v2.py`.** v11.3's cover-pairing lock was scoped to the ENTIRE `post_cover → send → wait-for-PDF` chain, which serialized the queue behind Bot 2's response time. v12.33 splits the boundary: the Bot 2 send + `wait_for_pdf` run UNLOCKED (real parallelism), while the two DB-channel writes (post cover, forward PDF) run under `pool.channel_write()`. The channel therefore always reads `cover_A, pdf_A, cover_B, pdf_B` — never interleaved. The pairing race that used to justify the wide lock is now closed by `bot2_client._last_sent_msg_id_by_client` being PER-CLIENT (see below).
- **`bot2_client.py`.** Module-global `_last_sent_msg_id` → per-client dict `_last_sent_msg_id_by_client` keyed on `id(client)`. Each userbot has its own DM history with Bot 2, so with 2 slots the old global would clobber the message-id floor across slots and reintroduce the v11.3 race. `send_link` now writes into the per-client entry; `wait_for_pdf` reads the floor for the client it was called with. Legacy `last_sent_msg_id()` with no arg still works.
- **`admin_bot.py`.** New `/checkram` command (admin-only via `@only_admin`). Walks `psutil.process_iter()`, matches BOT 0's three cmdlines (`uvicorn`, `worker.py`, `admin_bot.py`), sums RSS per label, and prints a code-block breakdown + total against the 512 MB ceiling. Also prints per-slot pool diagnostics (in_flight, total_fetches, total_floods, cooling seconds) so a FloodWait event is visible without tailing logs.
- **`requirements.txt`.** Adds `psutil>=5.9,<6.0` for `/checkram`.
- **`config.py`.** Adds a real `VERSION = "v12.33"` module constant + `settings.version` field so `/health` / `/checkram` / logs report it deterministically (comment-based v12.31/32 references were the only version signal before).
- **`start.sh` / `verify_v2.sh`.** Cosmetic bumps to v12.33; step 1b (one-shot session self-check) still validates slot 1's STRING_SESSION only, slot 2's STRING_SESSION_2 is validated inside worker.py by `UserbotPool.start()`.

### Env-var contract (BOT 0)
- **Slot 1 (unchanged, legacy):** `API_ID`, `API_HASH`, `STRING_SESSION`. Existing v12.32 env keeps working with zero migration.
- **Slot 2 (NEW, additive-only):** `STRING_SESSION_2`. Both userbots share the same `API_ID`/`API_HASH` (same Telegram dev app), so ONLY `STRING_SESSION_2` needs to be added in Render.
- **Future slots:** `STRING_SESSION_3`, `_4`, … Same rule. Any slot whose STRING_SESSION is missing/blank is silently skipped, so a solo-userbot deploy still boots.
- Session generation for slot 2: run `scripts/gen_session.py` with the second Telegram account and paste the output into Render as `STRING_SESSION_2`.

### Cover ↔ PDF ordering guarantee
1. **Fetch (unlocked, per-slot):** `bot2_client.send_link(client, bot2, url)` → `wait_for_pdf(client, bot2, since_ts, timeout)`. Two slots' waits overlap; the per-client message-id floor keeps their DMs from cross-contaminating.
2. **Channel writes (locked, pool-global):** `async with pool.channel_write(): post_cover(...) ; forward_messages(bot2_msg)`. One `asyncio.Lock` on the pool, one DB channel, one lock — no per-channel structure (per v12.33 briefing).
3. **User-facing "queued" ack:** still fires to the requester's DM via `progress_tracker` / `_auto_dm_requester` — this was already the case, cover_poster does not touch the user DM.
4. **On scrape/cover failure AFTER Bot 2 was already contacted:** the pending `wait_task` is cancelled, the URL sent to Bot 2 is orphaned, and its late reply is ignored by the per-client id floor. Tombstoned `FAILED_SCRAPE`.

### FloodWait handling
- `pool.mark_flood(slot, seconds, context=...)` cools that slot until `time.monotonic() + seconds`.
- Dispatcher skips cooling slots; the current job is failed/re-queued and the next job auto-lands on a healthy slot.
- **Admin alert** (Bot API `sendMessage` to `ADMIN_USER_ID`) fires on entry to cooling: `⚠️ Userbot slot N cooling for Ss — FloodWait from @Gallery_DLBot`.
- Slot exits cooling automatically when `cooling_until` passes; no explicit "recovered" alert (kept simple per v12.33 briefing).

### /checkram output shape
```
📊 RAM Usage
• uvicorn     212.4 MB
• worker      118.7 MB
• admin_bot    34.1 MB

🤖 Userbot pool
• slot 1  in_flight=0  fetches=142  floods=1
• slot 2  in_flight=1  fetches=138  floods=0
──────────────────────
• TOTAL      365.2 MB / 512.0 MB [71.3%]
```
Admin-only. Silent for non-admins (`@only_admin` gate). If `psutil.process_iter` finds no siblings (odd cmdline), falls back to `psutil.Process().memory_info().rss` for this process and says so.

### Rollout (per v12.33 briefing)
- Ships as **v12.33** with the pool **always on**. Single-userbot code path (`build_client()` inside `worker._run_loop`) is DELETED — no feature flag, no dead code. `build_client()` in `userbot.py` is retained ONLY because `start.sh` step 1b (one-shot session self-check) still uses it for slot 1's session.
- **Emergency rollback:** unset `STRING_SESSION_2` in Render and redeploy — the pool boots with 1 slot, behaviour is byte-equivalent to v12.32 for ordering (single slot never contends the channel lock) but keeps v12.33's per-client id-floor fix.

### Locked next task from HANDOVER §13
- **DONE.** Multi-userbot pool shipped in v12.33. Next task is unlocked; ask Ryan for the next brief.

## v12.33b — concurrent dispatch fix (2026-08-21)

### Symptom
Prod logs showed `v12.33: userbot pool ready with 2 slot(s)` but every job (2836–2845) logged `dispatched to userbot slot 1`. Slot 2 never received work.

### Root cause
The first v12.33 `worker.py` main loop still ran jobs **serially**: `await process_job(...)` blocked the loop for the whole job lifetime, so at each dispatch `pool.acquire()` saw every slot with `in_flight=0` and the tie-break always picked slot 1. The pool existed but could never parallelise.

### Fix
- `worker.py` `_run_loop` is now a **dispatcher**: it spawns one `asyncio.Task` per job (`_run_one_job`), bounded by `max_concurrent = len(pool.slots)`, and keeps pulling while there is capacity.
- Gates, in order: reap finished tasks → pause flag → capacity → `pool.has_healthy_slot()` → `db.next_pending` → `mark_processing` (synchronous, so the next loop can't double-pull the row) → `create_task`.
- `_run_one_job` holds the slot for the whole job (`async with pool.acquire()`), then applies the outcome (status, token refund, terminal progress phase, batch counters) exactly as the serial loop did. Auth/session failures set a `fatal` event; the dispatcher drains in-flight tasks and exits 4.
- Batch summary fires only when the queue is drained AND `in_flight` is empty. On SIGTERM, in-flight jobs are drained via `asyncio.gather` before `pool.stop()`.
- **Inter-job delay removed when pooled** (user decision 2026-08-21): `if max_concurrent == 1: await _random_delay()` keeps the v12.32 pacing only in 1-slot rollback mode.
- `userbot_pool.py`: added `has_healthy_slot()` so the dispatcher doesn't pull a job it can't dispatch while all slots cool.

## v12.34 — status badges + atomic channel pairing (2026-08-21)

### Task 1 — ⚡⚡ / 📥 card badges (backend + frontend)
- **`db.py`** — new `get_cached_gallery_ids(conn, ids) -> set`: ONE Mongo `find` against `galleries.{_id, status}` (covered by the `_id` index) returning the subset of ids in status `COMPLETED`. Silent-fail (returns empty set on any error).
- **`miniapp/backend/app/routes/_badge.py`** (NEW) — shared `attach_is_cached(items)` helper. Mutates each dict item in place to add `is_cached: bool`. Never raises; badges are cosmetic and must never 500 a list route.
- **Wired into every card-list route**: `search.py`, `bookmarks.py`, `recommendations.py`, `random.py` (single-item path via `attach_is_cached([pick])`), `trending.py`.
- **`miniapp/frontend/js/components/card.js`** — renders a `<span class="status-pill cached|uncached">` top-right of the cover when `props.is_cached` is a boolean: `⚡⚡` when true, `📥` when false. No pill when the field is absent (legacy endpoints).
- **`miniapp/frontend/css/components.css`** — `.status-pill` rule: `rgba(0,0,0,0.6)` background, 10px radius, `pointer-events:none` so taps pass through to the card. If a legacy `.badge` and the new pill coexist, the pill drops 22px to avoid overlap.
- **Badge semantics**: `COMPLETED` only counts as cached (per user decision 2026-08-21). PROCESSING / PARTIAL / FAILED do NOT show ⚡⚡.

### Task 2 — atomic cover+PDF channel pairing (the "jumbled channel" fix)
- **Symptom** (prod screenshot 2026-08-21): DB channel showed `cover_A, cover_B` back-to-back, then `pdf_A, pdf_B` back-to-back. Root cause: v12.33 held the pool's `channel_write()` lock TWICE per job — once for the cover post (step 3d), once for the PDF forward (step 6). Two slots could interleave their writes across those windows.
- **`cover_poster.py` split** into `prepare_cover(url, requester_handle)` (scrape + caption + cover download, NO channel write; returns `PreparedCover` holding caption + bytes + meta) and `post_prepared_cover(client, prepared, channel_id)` (the channel send only). Legacy `post_cover(client, url, ...)` kept as a thin wrapper so old call sites are byte-identical to v12.33.
- **`relay_v2.py` reordered** to: dedup → resolve entities → `prepare_cover` (unlocked) → `send_link` to Bot 2 (unlocked) → `wait_for_pdf` (unlocked — parallelism lives here) → on OK, **ONE** `async with pool.channel_write():` window containing `post_prepared_cover` immediately followed by `forward_messages(pdf)`. The channel therefore always reads `cover_A, pdf_A, cover_B, pdf_B`.
- **Side effects (user-approved)**: on Bot 2 timeout / error / scrape failure, NOTHING is posted to the channel — no orphan covers, nothing to roll back. If the cover post succeeds but the PDF forward fails inside the lock, the cover is deleted before releasing the lock (channel stays clean).
- v12.33's cancel-and-restart `wait_task` hack is gone: `prepare_cover` knows the page count BEFORE Bot 2 is contacted, so the adaptive `_bot2_timeout_sec(pages)` is correct on the first call.
- RAM: at most `pool_size × ~1 MB` of cover bytes held during the Bot 2 wait (~2 MB worst case at pool_size=2) — negligible against the 512 MB ceiling.

### Rollout
- Ships as **v12.34** (`config.VERSION = "v12.34"`). No new env vars. No new processes. Deploy = push to `main`, both Render services redeploy from the same commit.

## v12.39.5 — nhentai_cache import-depth fix (2026-08-22)

- Symptom: dedup sweep tick logs every ~6 min `⚠️ turso: nhentai_cache import failed: cannot import name 'db' from 'miniapp.backend.app.services'`; turso scanned/removed counters frozen at 0 (Turso-side dedup silently disabled, false-positive "everything fresh").
- Root cause: v12.39 bm_cover helpers used `from . import db as _midb` — `.` resolves to `miniapp.backend.app.services` (no db module). The Mongo shim lives one level up at `miniapp.backend.app.db`.
- Fix: one line, `miniapp/backend/app/services/nhentai_cache.py:38` → `from .. import db as _midb`.
- Verification: `py_compile` OK; `from miniapp.backend.app.services.nhentai_cache import bm_cover_get, bm_cover_put` OK.
- Commit: `19e2546` (local; push to main pending operator GitHub auth — sandbox has no credentials).

## v12.34b — cross-bot user-hint hook (2026-08-22)

### What changed
- **`bot0_hints.py` (NEW, repo root).** BOT 0-side hint publisher. `_get_client()` lazily creates a `MongoClient` against the existing `MONGO_URI` / `MONGO_DB_NAME` env; `hint_push_gid(gid)` does an atomic `$push` followed by a `$slice`-with-negative-cap trim to 200 entries on the `scraper1_state` doc `_id="user_gallery_hints"`. Silent on any Mongo failure — never affects the request hot path.
- **`ScraperBot/app/mongo_client.py`.** New functions `hint_pop_gids(n=4)` (one atomic `find_one_and_update` with `$slice` positive-start) and `hint_queue_size()`. Both never raise; both return empty / 0 on Mongo outage.
- **`ScraperBot/app/services/details_sweeper.py`.** `sweep_once()` gets a new step **0** that runs BEFORE the existing priority-hints drain: `mongo_client.hint_pop_gids(min(details_per_tick, 4))` → `_work_external_hints(client, gids)` → each gid runs through the existing `_fetch_one_gallery(client, gid, source_sort="user-hint")` path. The "user-hint" label rolls up under a dedicated row in the channel dashboard so you can see it (and only it) trend up after a deploy.
- **`miniapp/backend/app/services/scraper_bridge.py`.** `_hint_push(gid)` helper, lazily imports `bot0_hints` so a missing module never breaks the request. Called at **both** cache-write points: list-page Turso write (one hint per item, capped by the existing queue size) and gallery-detail Turso write (one hint for the opened gallery).

### Storage shape
- Bot 0 writes: `db["relaybot"]["scraper1_state"].update_one({"_id":"user_gallery_hints"}, {"$push":{"value":<gid>},…}, upsert=True)`, then a gravity `$slice: [<arr>, -200]`.
- Bot 1 reads: `find_one_and_update({"_id":"user_gallery_hints"}, {"$set":{"value":{"$slice":[<arr>, n, 99999]}}}, return_document=AFTER)` returns the popped prefix; the doc now holds the remainder (FIFO eviction, atomic).

### Rollout
- Ships as **v12.34b**. No new env vars. No new collections. No new processes. Deploy = copy the affected 4 files over repo root, commit, push. Both Render services redeploy from the same commit.
- Verification log signature post-deploy:
  - BOT 0 Render: `🔔 bot0_hints: hint pushed gid=<N>` (debug; one per cold MISS write)
  - BOT 1 Render: `v12.34b user-hints: popping <n> gid(s): <id1>,<id2>,…` once per sweep tick that had hints
  - Channel dashboard: a new `user-hint` row under the per-sort block; only ticks UP when BOT 0 fires — confirms end-to-end.

### Locked next task
- Patch shipped + verified compile. Awaiting operator's next brief.

## v12.34c — silent cache-miss fix (2026-08-22)

### Symptom
Same gallery ID clicked 3+ times in 2 minutes; every open logged
`🌐 [CACHE MISS] Fetched from upstream nhentai and cached to Turso  key=gallery:674790`
followed by `📝 [TURSO WRITE] … bytes=2069`. Never `⚡ [TURSO CACHE HIT]`
for any `gallery:<id>` in the window. Chip/tag `search:*` rows kept
hitting fine.

### Root cause (verified with a direct Turso probe against the live DB)
- Row `gallery:674790` **was** physically present in `nhentai_cache` after
  the first write (2069-byte JSON payload, `expires_at` 29.5 days in the
  future, `ttl_sec=2592000`, payload deserialises to a dict with `.id`).
- 6 419 total fresh `gallery:*` rows in the same table, 0 expired.
- BOT 0's `_turso_get()` path returned `None` on the immediate re-read
  anyway, and every failure mode was silently swallowed (no
  log line to disambiguate). The two known write-then-read failure
  surfaces in libsql-client 0.3.x are (a) `read_your_writes` default OFF
  meaning a fresh client GET can hit a replica that hasn't replicated,
  and (b) the per-call `create_client → execute → close` race can return
  a ResultSet without `.rows` before the response has fully drained.

### Fix
- **`miniapp/backend/app/services/turso_client.py`.** `_make_client()`
  now passes `read_your_writes=True` to `libsql_client.create_client(…)`
  with a graceful `TypeError` fallback if the installed libsql_client is
  too old to accept the kwarg (logs one INFO line prompting an upgrade).
  Also: `execute()` now logs `WARNING` when the coroutine returns None
  instead of silently returning None — the log will tell you WHICH mode
  failed on the next miss.
- **`miniapp/backend/app/services/nhentai_cache.py`.** `_turso_get()`
  gains 5 named diagnostic branches: turso unavailable, execute raised,
  `rs is None`, `rs.rows` empty (legit cold miss), `expires_at` unparseable,
  row expired, payload not JSON. Each returns None but logs `key` +
  reason first so a `grep 'turso_get(gallery:'` on Render tells the
  operator exactly why a MISS fired.
- **`miniapp/backend/app/services/scraper_bridge.py`.** `_direct_nhentai_detail`
  now logs a WARNING when `_nhc.get()` returned something non-None but
  the `isinstance(dict) and has_id` gate rejected it — disambiguates
  bad-payload from cold-miss.

### Verification signature after deploy
- BOT 0 Render on a warm gallery: single `⚡ [TURSO CACHE HIT] key=gallery:<N>`, NO followup MISS/WRITE for the same id.
- On a genuine cold miss: `turso_get(gallery:<N>): rs.rows empty (row not in table)` (DEBUG) → MISS → WRITE.
- On a libsql transport error: `turso_get(gallery:<N>): rs is None` (WARNING) → tells us to upgrade the client.
- On a bad payload: `turso_get(gallery:<N>): payload not JSON-parseable` (WARNING) with a 120-char head so we can inspect.

### Rollout
- Ships as **v12.34c**. No new env vars. No new deps. Deploy = copy 3
  files over `miniapp/backend/app/services/`, commit, push. Only BOT 0
  redeploys (nothing in `ScraperBot/` changed).

## v12.34d — Mongo `expires_at` datetime coercion + stale-row purge (2026-08-22)

### Symptom (v12.34c log surfaced it)
Immediately after v12.34c deploy, opening `gallery:427795` twice logged:
```
🌐 [CACHE MISS] key=gallery:427795         (open 1 — legitimate cold)
📝 [TURSO WRITE] key=gallery:427795 bytes=…
🌐 [CACHE MISS] key=gallery:427795         (open 2 — should have been HIT)
WARNING miniapp.scraper nhc.get(gallery:427795) raised:
    can't compare offset-naive and offset-aware datetimes
```

### Root cause
`nhentai_cache._mongo_get()` line 259 previously did `exp > now` where
`now = _now_dt()` is timezone-aware but `exp` from a pre-v12.4 Mongo
row was a **naive** `datetime.utcnow()` value. Python raises
`TypeError` on that comparison. The exception propagated through
`nhentai_cache.get()` → the v12.34c wrapper in `scraper_bridge` caught
it → returned None → fell through to nhentai upstream → refetch.

Turso reads worked fine (row was present, fresh, v12.34c
`read_your_writes=True` in effect). The bug was in the **Mongo
fallback** path: for every gallery ID that still had a stale row in
the legacy `nhentai_cache` collection, the read path crashed with a
type error and re-fetched upstream — even though Turso had it.

### Fix
- **`miniapp/backend/app/services/nhentai_cache.py::_mongo_get`.**
  Coerce every stored `expires_at` shape to epoch float BEFORE any
  comparison: `int|float` → passthrough; `datetime` with `tzinfo` →
  `.timestamp()`; naive datetime → assume UTC and coerce; anything
  else → log-warn and treat as expired. Never raises.
- **`scripts/purge_mongo_nhentai_cache.py` (NEW).** One-shot
  maintenance script that deletes every row from the Mongo
  `nhentai_cache` collection. Rationale: since v12.4 both bots have
  Mongo writes gated OFF; the remaining rows are pre-v12.4 leftovers
  serving no purpose. Ships with `DRY_RUN=1` mode and prints
  before/after row counts.

### Verification signature after deploy + purge
- Second open of the same gallery id → `⚡ [TURSO CACHE HIT]` (no
  followup MISS/WRITE).
- If a poisoned row somehow re-appears, the log now says
  `WARNING mongo_get(<key>): expires_at has unexpected type <T> …` or
  `WARNING mongo_get(<key>): expires_at datetime coerce failed …`
  instead of raising.
- Purge script: `MONGO_URI=... python scripts/purge_mongo_nhentai_cache.py`
  prints `deleted: N` and `rows after: 0`. Run once; idempotent.

### Rollout
- Ships as **v12.34d**. No new env vars. No new deps. Only BOT 0
  redeploys (nothing in `ScraperBot/` changed). After the redeploy,
  run the purge script ONCE with `MONGO_URI` in your Render shell (or
  locally) to clear the stale collection.

## v12.34l — inline-loader z-index fix (2026-08-23)

### Symptom
Detail sheet open → tap Download Now → "Sending to your DM…" pill rendered BEHIND the sheet backdrop instead of overlaying it.

### Root cause
`.inline-loader` z-index was `calc(var(--du-z-tabbar, 40) + 5)` = 55, but the detail sheet sits at `--du-z-sheet` = 200. The pill painted under the backdrop.

### Fix
One line: `z-index: calc(var(--du-z-toast, 300) + 1)` = 301 in `miniapp/frontend/css/loader-hourglass.css`. Pill now overlays the sheet at the same top-center layer as the "📨 Sent to your DM" toast.

### Rollout
Copy file over repo, commit, push. BOT 0 redeploys. No env changes.

================================================================================
v1.22 — BackupDB: high-availability Database Channel (2026-08-27)
================================================================================
Secondary private channel (BackupDB) mirrors every cover+PDF posted to the
Main Database Channel; BackupDB message ids are stamped onto the SAME Mongo
galleries doc (backup_channel_id / backup_cover_msg_id / backup_pdf_msg_id /
backup_status / backed_up_at). New files: backup_db.py (Bot 0 helpers),
scripts/backfill_backup_channel.py (one-time, runs on operator's PC,
Mongo-driven, batched, resumable, integrity-checked). Patched: relay_v2.py
(mirror after locked write + use_backup-aware auto-DM), admin_bot.py
(/usebackupDB on|off|status), config.py (BACKUP_DB_CHANNEL_ID optional),
db.py (backup_state accessor), Bot2Fetcher/app/fetcher.py (self-contained
mirror twin — it cannot import repo-root modules).

DISASTER RECOVERY: /usebackupDB on → delivery forwards from BackupDB.
PROMOTION RUNBOOK (BackupDB → new Main): 1) /usebackupDB on (users keep
downloading). 2) Set DATABASE_CHANNEL_ID=<BackupDB id> on Bot 0 and
DB_CHANNEL_ID=<BackupDB id> on Bot2Fetcher env vars. 3) /usebackupDB off.
4) Unset BACKUP_DB_CHANNEL_ID everywhere and clear Mongo backup_state
backup_channel_id. 5) Re-run scripts/backfill_backup_channel.py — it
auto-creates a FRESH BackupDB against the new Main; galleries already
stamped get a new backup copy (clear backup_* fields first if you want a
full re-mirror). 6) Re-pin BACKUP_DB_CHANNEL_ID with the new id.
================================================================================

v1.22.1 HOTFIX (2026-08-28): Mini App dedup-deliver path (POST
/api/queue/deliver/<gid> → miniapp/backend/app/services/dm_delivery.py)
bypassed the BackupDB toggle and always copied from Main. dm_delivery.py
now reads backup_state.use_backup and, when ON with a full backup pair on
the galleries doc, copies cover+PDF from BackupDB. Cover-only backups stay
on Main deliberately (never mix source channels in one delivery).

v1.22.2 (2026-08-28): 1) ScraperBot webhook keeper — set env
BOT1_PUBLIC_BASE_URL (e.g. https://scraperbackup.onrender.com) and
main.py auto-registers the /telegram?s=<BOT1_WEBHOOK_SECRET> webhook at
boot and re-verifies every 6h. Ends manual setWebhook after token or
Render-account changes (and prevents the 0-vs-O OCR incident — the URL
is built from the env var itself). 2) Bot2Fetcher dashboard now wraps its
message in a Markdown v1 code fence (quoted look, same as Bot 1) plus a
plain-text fallback retry — gallery titles can no longer cause the
400 "can't parse entities" spam.

v1.22.3 (2026-08-28): Bot2Fetcher caption cleanup. _clean_title in
Bot2Fetcher/app/meta.py now strips EVERY [..] / (..) / {..} segment
anywhere in the title (innermost-first, so nested "[Circle (Artist)]"
forms fully disappear), collapses the gaps left behind, and never ships
an empty title. Tags row now uses ➲ instead of ➤. Verified: "[Kuroiwa
Menou] Hitozuma to Shounen ... (Hitozuma Club Glass no Kutsu) [English]
{Zombii}" → "Hitozuma to Shounen Hirusagari no Yuuwaku | Married Woman
and Boy: Early Afternoon Temptation". Affects NEW cover posts only —
already-posted captions in the channels are unchanged.

v1.22.4 (2026-08-29): ScraperBot stability + bandwidth. 1) Mongo index
ensure + Turso bootstrap moved off the startup path into a background task
(main.py _bg_db_warmup) — Render's 5s /healthz probe can no longer time
out during boot (the Deploy-failed / Instance-failed loop). 2) OOM taming:
raw search-page JSON released right after normalize + gc.collect() between
pages. 3) Per-sort freshness scheduling: date=2h (pages 1-5, deep 1-15
daily), popular-today=6h, popular-week=12h, popular=24h, tag:*=24h — all
env-overridable (LIST_TICK_*_SEC). Phases skip fresh sorts entirely and
the gap sleeps only until the next sort is due. 4) Channel dashboard shows
an explicit idle line ("💤 idle — all sorts fresh") between phases instead
of a frozen "Now:" row. UptimeRobot: monitor https://<service>/healthz
GET every 5 min to prevent idle spindown (cannot fix crash loops — that
was the deploy blocker this version fixes).

v1.22.4 (2026-08-29): ScraperBot stability + bandwidth. 1) Mongo index
ensure + Turso bootstrap moved off the startup path into a background task
(main.py _bg_db_warmup) — Render's 5s /healthz probe can no longer time
out during boot (the Deploy-failed / Instance-failed loop). 2) OOM taming:
raw search-page JSON released right after normalize + gc.collect() between
pages. 3) Per-sort freshness scheduling: date=2h (pages 1-5, deep 1-15
daily), popular-today=6h, popular-week=12h, popular=24h, tag:*=24h — all
env-overridable (LIST_TICK_*_SEC). Phases skip fresh sorts entirely and
the gap sleeps only until the next sort is due. 4) Channel dashboard shows
an explicit idle line ("💤 idle — all sorts fresh") between phases instead
of a frozen "Now:" row. UptimeRobot: monitor https://<service>/healthz
GET every 5 min to prevent idle spindown (cannot fix crash loops — that
was the deploy blocker this version fixes).

v1.22.6 (2026-08-29): 1) Mini App gray "›" next button after page 2 — REAL
fix. The aggregation loop in scraper_bridge.py stops the instant
collected == window size (page 2 = upstream pages 1+2 = 50 = exactly
start_offset+per_page), so the cushion-based has_more was always False.
New: after slicing the window we probe the NEXT chip cache key
(search:<sort>:page<upstream_page>) in Turso — one cheap GET, stale rows
count under USE_OLD_CACHE — and OR that into has_more. 2) Dedup-sweep DM
spam: dedup_cron._is_real_error now treats pymongo network/serverSelection
timeouts as transient (Atlas M0 idle-closes connections; the next sweep
reconnects) — no more identical "🧹 Dedup sweep ⚠ timed out" DMs every
12h. Real errors still alert. v1.22.5 USE_OLD_CACHE confirmed live in
Render log (CACHE HIT pages 1+2 on every sort).

v1.22.7 (2026-08-29): gray › button on popular-today / popular-week / date —
root-caused and fixed properly. TWO interacting bugs: (1) Backend
scraper_bridge.py collected exactly the window worth of upstream pages,
THEN v12.30 id-dedup shrank it (volatile nhentai lists share 5-8 galleries
between adjacent pages) — short windows (page 2 showing only 3 cards) and
has_more=False. Stable all-time "popular" had near-zero overlap so only it
worked. Fix: collect one extra upstream page (collect_goal =
want_total + per_page; cache-hit when warm) and fire the next-page
lookahead probe whenever the window is non-empty, not only at exactly 25
cards. (2) Frontend search.js leaked highestKnownPage/knownLastPage across
sorts (never reset in refetch) — browsing popular to page 15 "unlocked"
the numbered bar on other sorts. Fix: both bounds reset on refetch().

v1.22.8 (2026-08-29): ScraperBot status-137 OOM / health-check crash loop.
1) Staggered starts: db warmup waits 5s (split into Mongo indexes, then
+20s, then Turso schema), list_sweeper 30s, details_sweeper 60s, dashboard
45s — boot memory peaks no longer overlap on the 512MB free instance.
2) Dashboard default refresh 5s → 15s (BOT1_CHANNEL_REFRESH_SEC; /time
still overrides at runtime). 3) hf_scraper_lite.make_client caps the
httpx pool at 4 connections (default was 100 keep-alives — socket buffers
multiplied RSS during bursts). 4) RAM watchdog task logs VmRSS every 60s
(warn 380MB / critical 450MB) so OOM approaches are visible BEFORE the
SIGKILL. 5) New admin command /checkram — live VmRSS + headroom in chat.
NOTE: UptimeRobot prevents idle spindown only; it could never stop these
OOM kills — the stagger + pool cap address the actual cause.

v1.22.9 (2026-08-29): THE ScraperBot crash-loop root cause — sync Mongo on
the event loop. Evidence: watchdog showed RAM steady at 76-84MB (RAM was
never the problem) while Render logged graceful SIGTERM shutdowns every
2-5 min, each after /healthz went unanswered >5s. The culprits:
record_activity() (every fetch), is_paused() (every loop iteration),
_stats_bump() (every write) and the dashboard tick all call synchronous
pymongo (state_get/state_set) directly on the uvicorn loop; one cold Atlas
M0 socket blocks up to serverSelectionTimeoutMS=8000 — longer than
Render's 5s health window. Fix: scraper1_state helpers now use an
in-process read-through cache (8s TTL, daemon-thread refresh; writes
update the cache synchronously then persist in background threads). No
event-loop path ever blocks on Mongo again. /checkram and the 60s RAM
watchdog stay as the monitoring layer that proved the diagnosis.

v1.23.1 (2026-08-29): ScraperBot MEMORY LEAK fix (76MB → 465MB climb, then
stuck at CRITICAL 40+ min). Root cause: v1.22.9's background state threads
were unbounded (one daemon thread per stale read/write) AND the MongoClient
had no socketTimeoutMS — a thread reading on a half-dead Atlas socket
blocked FOREVER (server selection has an 8s cap but socket reads did not),
so threads piled up during every Mongo slow phase; each held its stack and
pymongo buffers, RSS never released. Three fixes in
ScraperBot/app/mongo_client.py: 1) socketTimeoutMS=20000 — any blocked
socket op dies in 20s and its thread exits. 2) _STATE_INFLIGHT guard — at
most ONE background refresh/persist thread per key, ever. 3) 10-minute
eviction sweep on the state cache. Ship together with v1.23.0 countdowns.
# DoujinshiUniverse — Setup & Deploy Guide

## Repo layout
- Repo root = **BOT 0** (DoujinshiUniverse user-facing service): mini-app backend (FastAPI) + worker + admin bot.
- `ScraperBot/` = **BOT 1** (cache warmer; no user surface; deployed separately, Render Root Directory must be `ScraperBot`).
- BOT 2 = external `@Gallery_DLBot` (not in this repo).

## BOT 0 deploy (Render)
- Start command: `bash start.sh`
- Boot order: env check → Mongo check → one-shot Telethon session check (userbot.py, then exits) → supervised `admin_bot.py` + `worker.py` → uvicorn foreground on `$PORT`.
- 3 resident processes (v12.31+): uvicorn (foreground), admin_bot, worker. relay.py (legacy V1) was removed.
- Env required: API_ID, API_HASH, BOT_TOKEN (or ADMIN_BOT_TOKEN), MONGO_URI, STRING_SESSION, ADMIN_USER_ID, BOT2_USERNAME, DATABASE_CHANNEL_ID, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN.
- Monitors/health checks must hit `/healthz` (HEAD `/` returns 405).

## BOT 1 deploy (Render)
- New Web Service, same repo, Root Directory = `ScraperBot`, region as needed.
- Env: MONGO_URI, MONGO_DB_NAME, TURSO_DATABASE_URL, TURSO_AUTH_TOKEN, BOT1_TOKEN, BOT1_LOG_CHANNEL_ID, BOT1_ADMIN_KEY (+ pacing knobs; `BOT1_REGION=ap-singapore` on the Singapore service for the region-split bucket).

## Cache contract (BOT 0 reads these keys exactly)
- `search:<sort>:page<N>` (chip pages), `search:q=<query>|sort=<s>|page=<N>` (typed/tag, query lowercase+collapsed), `gallery:<id>` (detail).
- `search:*` payload must be a LIST of card dicts; `gallery:*` must be a normalized dict with `id`, `title`, `tag_groups`, `page1_url`. Any other shape reads as MISS.

## Housekeeping rules
- All docs live in this ONE file. Do not add new per-version .md files.
- relay.py / main.py at root were deleted (v12.32) — do not restore; relay_v2.py and ScraperBot/main.py are the live ones.
- To revert, `git checkout <commit>^ -- <path>` or redeploy the previous ZIP.

## v12.33 — multi-userbot pool (2026-08-20)

### What changed
- **`userbot_pool.py` (NEW).** `UserbotPool` owns N `telethon.TelegramClient` instances inside `worker.py`'s single asyncio loop. Least-in-flight dispatch over non-cooling slots. Zero extra resident processes — the 512 MB ceiling stays intact.
- **`worker.py`.** Boots a pool instead of a single client; each job runs under `async with pool.acquire() as slot`. When every slot is cooling, the job is re-queued and the loop sleeps briefly. On shutdown the whole pool is stopped.
- **`relay_v2.py`.** v11.3's cover-pairing lock was scoped to the ENTIRE `post_cover → send → wait-for-PDF` chain, which serialized the queue behind Bot 2's response time. v12.33 splits the boundary: the Bot 2 send + `wait_for_pdf` run UNLOCKED (real parallelism), while the two DB-channel writes (post cover, forward PDF) run under `pool.channel_write()`. The channel therefore always reads `cover_A, pdf_A, cover_B, pdf_B` — never interleaved. The pairing race that used to justify the wide lock is now closed by `bot2_client._last_sent_msg_id_by_client` being PER-CLIENT (see below).
- **`bot2_client.py`.** Module-global `_last_sent_msg_id` → per-client dict `_last_sent_msg_id_by_client` keyed on `id(client)`. Each userbot has its own DM history with Bot 2, so with 2 slots the old global would clobber the message-id floor across slots and reintroduce the v11.3 race. `send_link` now writes into the per-client entry; `wait_for_pdf` reads the floor for the client it was called with. Legacy `last_sent_msg_id()` with no arg still works.
- **`admin_bot.py`.** New `/checkram` command (admin-only via `@only_admin`). Walks `psutil.process_iter()`, matches BOT 0's three cmdlines (`uvicorn`, `worker.py`, `admin_bot.py`), sums RSS per label, and prints a code-block breakdown + total against the 512 MB ceiling. Also prints per-slot pool diagnostics (in_flight, total_fetches, total_floods, cooling seconds) so a FloodWait event is visible without tailing logs.
- **`requirements.txt`.** Adds `psutil>=5.9,<6.0` for `/checkram`.
- **`config.py`.** Adds a real `VERSION = "v12.33"` module constant + `settings.version` field so `/health` / `/checkram` / logs report it deterministically (comment-based v12.31/32 references were the only version signal before).
- **`start.sh` / `verify_v2.sh`.** Cosmetic bumps to v12.33; step 1b (one-shot session self-check) still validates slot 1's STRING_SESSION only, slot 2's STRING_SESSION_2 is validated inside worker.py by `UserbotPool.start()`.

### Env-var contract (BOT 0)
- **Slot 1 (unchanged, legacy):** `API_ID`, `API_HASH`, `STRING_SESSION`. Existing v12.32 env keeps working with zero migration.
- **Slot 2 (NEW, additive-only):** `STRING_SESSION_2`. Both userbots share the same `API_ID`/`API_HASH` (same Telegram dev app), so ONLY `STRING_SESSION_2` needs to be added in Render.
- **Future slots:** `STRING_SESSION_3`, `_4`, … Same rule. Any slot whose STRING_SESSION is missing/blank is silently skipped, so a solo-userbot deploy still boots.
- Session generation for slot 2: run `scripts/gen_session.py` with the second Telegram account and paste the output into Render as `STRING_SESSION_2`.

### Cover ↔ PDF ordering guarantee
1. **Fetch (unlocked, per-slot):** `bot2_client.send_link(client, bot2, url)` → `wait_for_pdf(client, bot2, since_ts, timeout)`. Two slots' waits overlap; the per-client message-id floor keeps their DMs from cross-contaminating.
2. **Channel writes (locked, pool-global):** `async with pool.channel_write(): post_cover(...) ; forward_messages(bot2_msg)`. One `asyncio.Lock` on the pool, one DB channel, one lock — no per-channel structure (per v12.33 briefing).
3. **User-facing "queued" ack:** still fires to the requester's DM via `progress_tracker` / `_auto_dm_requester` — this was already the case, cover_poster does not touch the user DM.
4. **On scrape/cover failure AFTER Bot 2 was already contacted:** the pending `wait_task` is cancelled, the URL sent to Bot 2 is orphaned, and its late reply is ignored by the per-client id floor. Tombstoned `FAILED_SCRAPE`.

### FloodWait handling
- `pool.mark_flood(slot, seconds, context=...)` cools that slot until `time.monotonic() + seconds`.
- Dispatcher skips cooling slots; the current job is failed/re-queued and the next job auto-lands on a healthy slot.
- **Admin alert** (Bot API `sendMessage` to `ADMIN_USER_ID`) fires on entry to cooling: `⚠️ Userbot slot N cooling for Ss — FloodWait from @Gallery_DLBot`.
- Slot exits cooling automatically when `cooling_until` passes; no explicit "recovered" alert (kept simple per v12.33 briefing).

### /checkram output shape
```
📊 RAM Usage
• uvicorn     212.4 MB
• worker      118.7 MB
• admin_bot    34.1 MB

🤖 Userbot pool
• slot 1  in_flight=0  fetches=142  floods=1
• slot 2  in_flight=1  fetches=138  floods=0
──────────────────────
• TOTAL      365.2 MB / 512.0 MB [71.3%]
```
Admin-only. Silent for non-admins (`@only_admin` gate). If `psutil.process_iter` finds no siblings (odd cmdline), falls back to `psutil.Process().memory_info().rss` for this process and says so.

### Rollout (per v12.33 briefing)
- Ships as **v12.33** with the pool **always on**. Single-userbot code path (`build_client()` inside `worker._run_loop`) is DELETED — no feature flag, no dead code. `build_client()` in `userbot.py` is retained ONLY because `start.sh` step 1b (one-shot session self-check) still uses it for slot 1's session.
- **Emergency rollback:** unset `STRING_SESSION_2` in Render and redeploy — the pool boots with 1 slot, behaviour is byte-equivalent to v12.32 for ordering (single slot never contends the channel lock) but keeps v12.33's per-client id-floor fix.

### Locked next task from HANDOVER §13
- **DONE.** Multi-userbot pool shipped in v12.33. Next task is unlocked; ask Ryan for the next brief.

## v12.33b — concurrent dispatch fix (2026-08-21)

### Symptom
Prod logs showed `v12.33: userbot pool ready with 2 slot(s)` but every job (2836–2845) logged `dispatched to userbot slot 1`. Slot 2 never received work.

### Root cause
The first v12.33 `worker.py` main loop still ran jobs **serially**: `await process_job(...)` blocked the loop for the whole job lifetime, so at each dispatch `pool.acquire()` saw every slot with `in_flight=0` and the tie-break always picked slot 1. The pool existed but could never parallelise.

### Fix
- `worker.py` `_run_loop` is now a **dispatcher**: it spawns one `asyncio.Task` per job (`_run_one_job`), bounded by `max_concurrent = len(pool.slots)`, and keeps pulling while there is capacity.
- Gates, in order: reap finished tasks → pause flag → capacity → `pool.has_healthy_slot()` → `db.next_pending` → `mark_processing` (synchronous, so the next loop can't double-pull the row) → `create_task`.
- `_run_one_job` holds the slot for the whole job (`async with pool.acquire()`), then applies the outcome (status, token refund, terminal progress phase, batch counters) exactly as the serial loop did. Auth/session failures set a `fatal` event; the dispatcher drains in-flight tasks and exits 4.
- Batch summary fires only when the queue is drained AND `in_flight` is empty. On SIGTERM, in-flight jobs are drained via `asyncio.gather` before `pool.stop()`.
- **Inter-job delay removed when pooled** (user decision 2026-08-21): `if max_concurrent == 1: await _random_delay()` keeps the v12.32 pacing only in 1-slot rollback mode.
- `userbot_pool.py`: added `has_healthy_slot()` so the dispatcher doesn't pull a job it can't dispatch while all slots cool.

## v12.34 — status badges + atomic channel pairing (2026-08-21)

### Task 1 — ⚡⚡ / 📥 card badges (backend + frontend)
- **`db.py`** — new `get_cached_gallery_ids(conn, ids) -> set`: ONE Mongo `find` against `galleries.{_id, status}` (covered by the `_id` index) returning the subset of ids in status `COMPLETED`. Silent-fail (returns empty set on any error).
- **`miniapp/backend/app/routes/_badge.py`** (NEW) — shared `attach_is_cached(items)` helper. Mutates each dict item in place to add `is_cached: bool`. Never raises; badges are cosmetic and must never 500 a list route.
- **Wired into every card-list route**: `search.py`, `bookmarks.py`, `recommendations.py`, `random.py` (single-item path via `attach_is_cached([pick])`), `trending.py`.
- **`miniapp/frontend/js/components/card.js`** — renders a `<span class="status-pill cached|uncached">` top-right of the cover when `props.is_cached` is a boolean: `⚡⚡` when true, `📥` when false. No pill when the field is absent (legacy endpoints).
- **`miniapp/frontend/css/components.css`** — `.status-pill` rule: `rgba(0,0,0,0.6)` background, 10px radius, `pointer-events:none` so taps pass through to the card. If a legacy `.badge` and the new pill coexist, the pill drops 22px to avoid overlap.
- **Badge semantics**: `COMPLETED` only counts as cached (per user decision 2026-08-21). PROCESSING / PARTIAL / FAILED do NOT show ⚡⚡.

### Task 2 — atomic cover+PDF channel pairing (the "jumbled channel" fix)
- **Symptom** (prod screenshot 2026-08-21): DB channel showed `cover_A, cover_B` back-to-back, then `pdf_A, pdf_B` back-to-back. Root cause: v12.33 held the pool's `channel_write()` lock TWICE per job — once for the cover post (step 3d), once for the PDF forward (step 6). Two slots could interleave their writes across those windows.
- **`cover_poster.py` split** into `prepare_cover(url, requester_handle)` (scrape + caption + cover download, NO channel write; returns `PreparedCover` holding caption + bytes + meta) and `post_prepared_cover(client, prepared, channel_id)` (the channel send only). Legacy `post_cover(client, url, ...)` kept as a thin wrapper so old call sites are byte-identical to v12.33.
- **`relay_v2.py` reordered** to: dedup → resolve entities → `prepare_cover` (unlocked) → `send_link` to Bot 2 (unlocked) → `wait_for_pdf` (unlocked — parallelism lives here) → on OK, **ONE** `async with pool.channel_write():` window containing `post_prepared_cover` immediately followed by `forward_messages(pdf)`. The channel therefore always reads `cover_A, pdf_A, cover_B, pdf_B`.
- **Side effects (user-approved)**: on Bot 2 timeout / error / scrape failure, NOTHING is posted to the channel — no orphan covers, nothing to roll back. If the cover post succeeds but the PDF forward fails inside the lock, the cover is deleted before releasing the lock (channel stays clean).
- v12.33's cancel-and-restart `wait_task` hack is gone: `prepare_cover` knows the page count BEFORE Bot 2 is contacted, so the adaptive `_bot2_timeout_sec(pages)` is correct on the first call.
- RAM: at most `pool_size × ~1 MB` of cover bytes held during the Bot 2 wait (~2 MB worst case at pool_size=2) — negligible against the 512 MB ceiling.

### Rollout
- Ships as **v12.34** (`config.VERSION = "v12.34"`). No new env vars. No new processes. Deploy = push to `main`, both Render services redeploy from the same commit.

## v12.39.5 — nhentai_cache import-depth fix (2026-08-22)

- Symptom: dedup sweep tick logs every ~6 min `⚠️ turso: nhentai_cache import failed: cannot import name 'db' from 'miniapp.backend.app.services'`; turso scanned/removed counters frozen at 0 (Turso-side dedup silently disabled, false-positive "everything fresh").
- Root cause: v12.39 bm_cover helpers used `from . import db as _midb` — `.` resolves to `miniapp.backend.app.services` (no db module). The Mongo shim lives one level up at `miniapp.backend.app.db`.
- Fix: one line, `miniapp/backend/app/services/nhentai_cache.py:38` → `from .. import db as _midb`.
- Verification: `py_compile` OK; `from miniapp.backend.app.services.nhentai_cache import bm_cover_get, bm_cover_put` OK.
- Commit: `19e2546` (local; push to main pending operator GitHub auth — sandbox has no credentials).

## v12.34b — cross-bot user-hint hook (2026-08-22)

### What changed
- **`bot0_hints.py` (NEW, repo root).** BOT 0-side hint publisher. `_get_client()` lazily creates a `MongoClient` against the existing `MONGO_URI` / `MONGO_DB_NAME` env; `hint_push_gid(gid)` does an atomic `$push` followed by a `$slice`-with-negative-cap trim to 200 entries on the `scraper1_state` doc `_id="user_gallery_hints"`. Silent on any Mongo failure — never affects the request hot path.
- **`ScraperBot/app/mongo_client.py`.** New functions `hint_pop_gids(n=4)` (one atomic `find_one_and_update` with `$slice` positive-start) and `hint_queue_size()`. Both never raise; both return empty / 0 on Mongo outage.
- **`ScraperBot/app/services/details_sweeper.py`.** `sweep_once()` gets a new step **0** that runs BEFORE the existing priority-hints drain: `mongo_client.hint_pop_gids(min(details_per_tick, 4))` → `_work_external_hints(client, gids)` → each gid runs through the existing `_fetch_one_gallery(client, gid, source_sort="user-hint")` path. The "user-hint" label rolls up under a dedicated row in the channel dashboard so you can see it (and only it) trend up after a deploy.
- **`miniapp/backend/app/services/scraper_bridge.py`.** `_hint_push(gid)` helper, lazily imports `bot0_hints` so a missing module never breaks the request. Called at **both** cache-write points: list-page Turso write (one hint per item, capped by the existing queue size) and gallery-detail Turso write (one hint for the opened gallery).

### Storage shape
- Bot 0 writes: `db["relaybot"]["scraper1_state"].update_one({"_id":"user_gallery_hints"}, {"$push":{"value":<gid>},…}, upsert=True)`, then a gravity `$slice: [<arr>, -200]`.
- Bot 1 reads: `find_one_and_update({"_id":"user_gallery_hints"}, {"$set":{"value":{"$slice":[<arr>, n, 99999]}}}, return_document=AFTER)` returns the popped prefix; the doc now holds the remainder (FIFO eviction, atomic).

### Rollout
- Ships as **v12.34b**. No new env vars. No new collections. No new processes. Deploy = copy the affected 4 files over repo root, commit, push. Both Render services redeploy from the same commit.
- Verification log signature post-deploy:
  - BOT 0 Render: `🔔 bot0_hints: hint pushed gid=<N>` (debug; one per cold MISS write)
  - BOT 1 Render: `v12.34b user-hints: popping <n> gid(s): <id1>,<id2>,…` once per sweep tick that had hints
  - Channel dashboard: a new `user-hint` row under the per-sort block; only ticks UP when BOT 0 fires — confirms end-to-end.

### Locked next task
- Patch shipped + verified compile. Awaiting operator's next brief.

## v12.34c — silent cache-miss fix (2026-08-22)

### Symptom
Same gallery ID clicked 3+ times in 2 minutes; every open logged
`🌐 [CACHE MISS] Fetched from upstream nhentai and cached to Turso  key=gallery:674790`
followed by `📝 [TURSO WRITE] … bytes=2069`. Never `⚡ [TURSO CACHE HIT]`
for any `gallery:<id>` in the window. Chip/tag `search:*` rows kept
hitting fine.

### Root cause (verified with a direct Turso probe against the live DB)
- Row `gallery:674790` **was** physically present in `nhentai_cache` after
  the first write (2069-byte JSON payload, `expires_at` 29.5 days in the
  future, `ttl_sec=2592000`, payload deserialises to a dict with `.id`).
- 6 419 total fresh `gallery:*` rows in the same table, 0 expired.
- BOT 0's `_turso_get()` path returned `None` on the immediate re-read
  anyway, and every failure mode was silently swallowed (no
  log line to disambiguate). The two known write-then-read failure
  surfaces in libsql-client 0.3.x are (a) `read_your_writes` default OFF
  meaning a fresh client GET can hit a replica that hasn't replicated,
  and (b) the per-call `create_client → execute → close` race can return
  a ResultSet without `.rows` before the response has fully drained.

### Fix
- **`miniapp/backend/app/services/turso_client.py`.** `_make_client()`
  now passes `read_your_writes=True` to `libsql_client.create_client(…)`
  with a graceful `TypeError` fallback if the installed libsql_client is
  too old to accept the kwarg (logs one INFO line prompting an upgrade).
  Also: `execute()` now logs `WARNING` when the coroutine returns None
  instead of silently returning None — the log will tell you WHICH mode
  failed on the next miss.
- **`miniapp/backend/app/services/nhentai_cache.py`.** `_turso_get()`
  gains 5 named diagnostic branches: turso unavailable, execute raised,
  `rs is None`, `rs.rows` empty (legit cold miss), `expires_at` unparseable,
  row expired, payload not JSON. Each returns None but logs `key` +
  reason first so a `grep 'turso_get(gallery:'` on Render tells the
  operator exactly why a MISS fired.
- **`miniapp/backend/app/services/scraper_bridge.py`.** `_direct_nhentai_detail`
  now logs a WARNING when `_nhc.get()` returned something non-None but
  the `isinstance(dict) and has_id` gate rejected it — disambiguates
  bad-payload from cold-miss.

### Verification signature after deploy
- BOT 0 Render on a warm gallery: single `⚡ [TURSO CACHE HIT] key=gallery:<N>`, NO followup MISS/WRITE for the same id.
- On a genuine cold miss: `turso_get(gallery:<N>): rs.rows empty (row not in table)` (DEBUG) → MISS → WRITE.
- On a libsql transport error: `turso_get(gallery:<N>): rs is None` (WARNING) → tells us to upgrade the client.
- On a bad payload: `turso_get(gallery:<N>): payload not JSON-parseable` (WARNING) with a 120-char head so we can inspect.

### Rollout
- Ships as **v12.34c**. No new env vars. No new deps. Deploy = copy 3
  files over `miniapp/backend/app/services/`, commit, push. Only BOT 0
  redeploys (nothing in `ScraperBot/` changed).

## v12.34d — Mongo `expires_at` datetime coercion + stale-row purge (2026-08-22)

### Symptom (v12.34c log surfaced it)
Immediately after v12.34c deploy, opening `gallery:427795` twice logged:
```
🌐 [CACHE MISS] key=gallery:427795         (open 1 — legitimate cold)
📝 [TURSO WRITE] key=gallery:427795 bytes=…
🌐 [CACHE MISS] key=gallery:427795         (open 2 — should have been HIT)
WARNING miniapp.scraper nhc.get(gallery:427795) raised:
    can't compare offset-naive and offset-aware datetimes
```

### Root cause
`nhentai_cache._mongo_get()` line 259 previously did `exp > now` where
`now = _now_dt()` is timezone-aware but `exp` from a pre-v12.4 Mongo
row was a **naive** `datetime.utcnow()` value. Python raises
`TypeError` on that comparison. The exception propagated through
`nhentai_cache.get()` → the v12.34c wrapper in `scraper_bridge` caught
it → returned None → fell through to nhentai upstream → refetch.

Turso reads worked fine (row was present, fresh, v12.34c
`read_your_writes=True` in effect). The bug was in the **Mongo
fallback** path: for every gallery ID that still had a stale row in
the legacy `nhentai_cache` collection, the read path crashed with a
type error and re-fetched upstream — even though Turso had it.

### Fix
- **`miniapp/backend/app/services/nhentai_cache.py::_mongo_get`.**
  Coerce every stored `expires_at` shape to epoch float BEFORE any
  comparison: `int|float` → passthrough; `datetime` with `tzinfo` →
  `.timestamp()`; naive datetime → assume UTC and coerce; anything
  else → log-warn and treat as expired. Never raises.
- **`scripts/purge_mongo_nhentai_cache.py` (NEW).** One-shot
  maintenance script that deletes every row from the Mongo
  `nhentai_cache` collection. Rationale: since v12.4 both bots have
  Mongo writes gated OFF; the remaining rows are pre-v12.4 leftovers
  serving no purpose. Ships with `DRY_RUN=1` mode and prints
  before/after row counts.

### Verification signature after deploy + purge
- Second open of the same gallery id → `⚡ [TURSO CACHE HIT]` (no
  followup MISS/WRITE).
- If a poisoned row somehow re-appears, the log now says
  `WARNING mongo_get(<key>): expires_at has unexpected type <T> …` or
  `WARNING mongo_get(<key>): expires_at datetime coerce failed …`
  instead of raising.
- Purge script: `MONGO_URI=... python scripts/purge_mongo_nhentai_cache.py`
  prints `deleted: N` and `rows after: 0`. Run once; idempotent.

### Rollout
- Ships as **v12.34d**. No new env vars. No new deps. Only BOT 0
  redeploys (nothing in `ScraperBot/` changed). After the redeploy,
  run the purge script ONCE with `MONGO_URI` in your Render shell (or
  locally) to clear the stale collection.

## v12.34l — inline-loader z-index fix (2026-08-23)

### Symptom
Detail sheet open → tap Download Now → "Sending to your DM…" pill rendered BEHIND the sheet backdrop instead of overlaying it.

### Root cause
`.inline-loader` z-index was `calc(var(--du-z-tabbar, 40) + 5)` = 55, but the detail sheet sits at `--du-z-sheet` = 200. The pill painted under the backdrop.

### Fix
One line: `z-index: calc(var(--du-z-toast, 300) + 1)` = 301 in `miniapp/frontend/css/loader-hourglass.css`. Pill now overlays the sheet at the same top-center layer as the "📨 Sent to your DM" toast.

### Rollout
Copy file over repo, commit, push. BOT 0 redeploys. No env changes.

================================================================================
v1.22 — BackupDB: high-availability Database Channel (2026-08-27)
================================================================================
Secondary private channel (BackupDB) mirrors every cover+PDF posted to the
Main Database Channel; BackupDB message ids are stamped onto the SAME Mongo
galleries doc (backup_channel_id / backup_cover_msg_id / backup_pdf_msg_id /
backup_status / backed_up_at). New files: backup_db.py (Bot 0 helpers),
scripts/backfill_backup_channel.py (one-time, runs on operator's PC,
Mongo-driven, batched, resumable, integrity-checked). Patched: relay_v2.py
(mirror after locked write + use_backup-aware auto-DM), admin_bot.py
(/usebackupDB on|off|status), config.py (BACKUP_DB_CHANNEL_ID optional),
db.py (backup_state accessor), Bot2Fetcher/app/fetcher.py (self-contained
mirror twin — it cannot import repo-root modules).

DISASTER RECOVERY: /usebackupDB on → delivery forwards from BackupDB.
PROMOTION RUNBOOK (BackupDB → new Main): 1) /usebackupDB on (users keep
downloading). 2) Set DATABASE_CHANNEL_ID=<BackupDB id> on Bot 0 and
DB_CHANNEL_ID=<BackupDB id> on Bot2Fetcher env vars. 3) /usebackupDB off.
4) Unset BACKUP_DB_CHANNEL_ID everywhere and clear Mongo backup_state
backup_channel_id. 5) Re-run scripts/backfill_backup_channel.py — it
auto-creates a FRESH BackupDB against the new Main; galleries already
stamped get a new backup copy (clear backup_* fields first if you want a
full re-mirror). 6) Re-pin BACKUP_DB_CHANNEL_ID with the new id.
================================================================================

v1.22.1 HOTFIX (2026-08-28): Mini App dedup-deliver path (POST
/api/queue/deliver/<gid> → miniapp/backend/app/services/dm_delivery.py)
bypassed the BackupDB toggle and always copied from Main. dm_delivery.py
now reads backup_state.use_backup and, when ON with a full backup pair on
the galleries doc, copies cover+PDF from BackupDB. Cover-only backups stay
on Main deliberately (never mix source channels in one delivery).

v1.22.2 (2026-08-28): 1) ScraperBot webhook keeper — set env
BOT1_PUBLIC_BASE_URL (e.g. https://scraperbackup.onrender.com) and
main.py auto-registers the /telegram?s=<BOT1_WEBHOOK_SECRET> webhook at
boot and re-verifies every 6h. Ends manual setWebhook after token or
Render-account changes (and prevents the 0-vs-O OCR incident — the URL
is built from the env var itself). 2) Bot2Fetcher dashboard now wraps its
message in a Markdown v1 code fence (quoted look, same as Bot 1) plus a
plain-text fallback retry — gallery titles can no longer cause the
400 "can't parse entities" spam.

v1.22.3 (2026-08-28): Bot2Fetcher caption cleanup. _clean_title in
Bot2Fetcher/app/meta.py now strips EVERY [..] / (..) / {..} segment
anywhere in the title (innermost-first, so nested "[Circle (Artist)]"
forms fully disappear), collapses the gaps left behind, and never ships
an empty title. Tags row now uses ➲ instead of ➤. Verified: "[Kuroiwa
Menou] Hitozuma to Shounen ... (Hitozuma Club Glass no Kutsu) [English]
{Zombii}" → "Hitozuma to Shounen Hirusagari no Yuuwaku | Married Woman
and Boy: Early Afternoon Temptation". Affects NEW cover posts only —
already-posted captions in the channels are unchanged.

v1.22.4 (2026-08-29): ScraperBot stability + bandwidth. 1) Mongo index
ensure + Turso bootstrap moved off the startup path into a background task
(main.py _bg_db_warmup) — Render's 5s /healthz probe can no longer time
out during boot (the Deploy-failed / Instance-failed loop). 2) OOM taming:
raw search-page JSON released right after normalize + gc.collect() between
pages. 3) Per-sort freshness scheduling: date=2h (pages 1-5, deep 1-15
daily), popular-today=6h, popular-week=12h, popular=24h, tag:*=24h — all
env-overridable (LIST_TICK_*_SEC). Phases skip fresh sorts entirely and
the gap sleeps only until the next sort is due. 4) Channel dashboard shows
an explicit idle line ("💤 idle — all sorts fresh") between phases instead
of a frozen "Now:" row. UptimeRobot: monitor https://<service>/healthz
GET every 5 min to prevent idle spindown (cannot fix crash loops — that
was the deploy blocker this version fixes).

v1.22.4 (2026-08-29): ScraperBot stability + bandwidth. 1) Mongo index
ensure + Turso bootstrap moved off the startup path into a background task
(main.py _bg_db_warmup) — Render's 5s /healthz probe can no longer time
out during boot (the Deploy-failed / Instance-failed loop). 2) OOM taming:
raw search-page JSON released right after normalize + gc.collect() between
pages. 3) Per-sort freshness scheduling: date=2h (pages 1-5, deep 1-15
daily), popular-today=6h, popular-week=12h, popular=24h, tag:*=24h — all
env-overridable (LIST_TICK_*_SEC). Phases skip fresh sorts entirely and
the gap sleeps only until the next sort is due. 4) Channel dashboard shows
an explicit idle line ("💤 idle — all sorts fresh") between phases instead
of a frozen "Now:" row. UptimeRobot: monitor https://<service>/healthz
GET every 5 min to prevent idle spindown (cannot fix crash loops — that
was the deploy blocker this version fixes).

v1.22.6 (2026-08-29): 1) Mini App gray "›" next button after page 2 — REAL
fix. The aggregation loop in scraper_bridge.py stops the instant
collected == window size (page 2 = upstream pages 1+2 = 50 = exactly
start_offset+per_page), so the cushion-based has_more was always False.
New: after slicing the window we probe the NEXT chip cache key
(search:<sort>:page<upstream_page>) in Turso — one cheap GET, stale rows
count under USE_OLD_CACHE — and OR that into has_more. 2) Dedup-sweep DM
spam: dedup_cron._is_real_error now treats pymongo network/serverSelection
timeouts as transient (Atlas M0 idle-closes connections; the next sweep
reconnects) — no more identical "🧹 Dedup sweep ⚠ timed out" DMs every
12h. Real errors still alert. v1.22.5 USE_OLD_CACHE confirmed live in
Render log (CACHE HIT pages 1+2 on every sort).

v1.22.7 (2026-08-29): gray › button on popular-today / popular-week / date —
root-caused and fixed properly. TWO interacting bugs: (1) Backend
scraper_bridge.py collected exactly the window worth of upstream pages,
THEN v12.30 id-dedup shrank it (volatile nhentai lists share 5-8 galleries
between adjacent pages) — short windows (page 2 showing only 3 cards) and
has_more=False. Stable all-time "popular" had near-zero overlap so only it
worked. Fix: collect one extra upstream page (collect_goal =
want_total + per_page; cache-hit when warm) and fire the next-page
lookahead probe whenever the window is non-empty, not only at exactly 25
cards. (2) Frontend search.js leaked highestKnownPage/knownLastPage across
sorts (never reset in refetch) — browsing popular to page 15 "unlocked"
the numbered bar on other sorts. Fix: both bounds reset on refetch().

v1.22.8 (2026-08-29): ScraperBot status-137 OOM / health-check crash loop.
1) Staggered starts: db warmup waits 5s (split into Mongo indexes, then
+20s, then Turso schema), list_sweeper 30s, details_sweeper 60s, dashboard
45s — boot memory peaks no longer overlap on the 512MB free instance.
2) Dashboard default refresh 5s → 15s (BOT1_CHANNEL_REFRESH_SEC; /time
still overrides at runtime). 3) hf_scraper_lite.make_client caps the
httpx pool at 4 connections (default was 100 keep-alives — socket buffers
multiplied RSS during bursts). 4) RAM watchdog task logs VmRSS every 60s
(warn 380MB / critical 450MB) so OOM approaches are visible BEFORE the
SIGKILL. 5) New admin command /checkram — live VmRSS + headroom in chat.
NOTE: UptimeRobot prevents idle spindown only; it could never stop these
OOM kills — the stagger + pool cap address the actual cause.

v1.22.9 (2026-08-29): THE ScraperBot crash-loop root cause — sync Mongo on
the event loop. Evidence: watchdog showed RAM steady at 76-84MB (RAM was
never the problem) while Render logged graceful SIGTERM shutdowns every
2-5 min, each after /healthz went unanswered >5s. The culprits:
record_activity() (every fetch), is_paused() (every loop iteration),
_stats_bump() (every write) and the dashboard tick all call synchronous
pymongo (state_get/state_set) directly on the uvicorn loop; one cold Atlas
M0 socket blocks up to serverSelectionTimeoutMS=8000 — longer than
Render's 5s health window. Fix: scraper1_state helpers now use an
in-process read-through cache (8s TTL, daemon-thread refresh; writes
update the cache synchronously then persist in background threads). No
event-loop path ever blocks on Mongo again. /checkram and the 60s RAM
watchdog stay as the monitoring layer that proved the diagnosis.

v1.23.1 (2026-08-29): ScraperBot MEMORY LEAK fix (76MB → 465MB climb, then
stuck at CRITICAL 40+ min). Root cause: v1.22.9's background state threads
were unbounded (one daemon thread per stale read/write) AND the MongoClient
had no socketTimeoutMS — a thread reading on a half-dead Atlas socket
blocked FOREVER (server selection has an 8s cap but socket reads did not),
so threads piled up during every Mongo slow phase; each held its stack and
pymongo buffers, RSS never released. Three fixes in
ScraperBot/app/mongo_client.py: 1) socketTimeoutMS=20000 — any blocked
socket op dies in 20s and its thread exits. 2) _STATE_INFLIGHT guard — at
most ONE background refresh/persist thread per key, ever. 3) 10-minute
eviction sweep on the state cache. Ship together with v1.23.0 countdowns.

v12.55 (2026-09-04): Turso read-budget triage — typed-search double-read
killed + Bot 2 scan memo. Context: Turso free tier bills ROWS TOUCHED (500M/
mo); usage hit 292M on Sep 4 (on pace for ~2.2B). Census (live, read-only):
nhentai_cache 18,930 rows = 15,048 gallery:* + 3,857 search:* (2,136 of them
LEGACY search:search:* — 55% of search rows — still probed on every typed
search). Fixes: 1) hf_scraper._turso_key_for maps typed-search cache keys
(search:<q>:p<N>) straight to the canonical search:q=…|sort=date|page=N row
— the legacy row is never read or written again (halves typed-search reads).
2) search() warm-probe now reads ONLY sort=date (was date+popular = 2 reads/
page) and accepts canonical LIST payloads — silences the "'list' object has
no attribute 'get'" WARNING and makes Bot 1's warm rows actually serve Bot 0
searches (title from title_en_clean, thumb from cover string, English filter
skipped — Bot 1 already filtered). 3) _fetch_json_cached writes L2 only for
canonical keys. 4) Bot2Fetcher list_gallery_ids got a 10s in-process memo
(BOT2_LIST_MEMO_TTL_SEC) — Turso bills rows TOUCHED not returned, and
cached_at was unindexed, so every "delta" scan walked all ~15k gallery rows;
plus CREATE INDEX idx_nhentai_cache_cached_at in ensure_schema so watermark
scans touch only new rows. 5) scripts/purge_legacy_search_keys.py — resumable
(cursor = remaining keys) batched purge of the 2,136 legacy rows; dry-run
mode included. NOTE: token-bucket consume was audited and left alone — it's
one atomic single-row UPDATE, already minimal; a memo there would break
cross-process quota. Cache contract: read shapes extended (list accepted),
written shapes unchanged. New tests/test_v12_55_read_budget.py (14 checks).

v12.56 (2026-09-04): Bot2Fetcher dashboard honesty fix. "Turso cache:
16,527" vs "In DB channel: 19,496" looked like data loss — it was a label
bug. cache_total is the POST-FILTER work queue (known-done/failed removed
in _build_queue_order), while "In DB channel" is the cumulative lifetime
COMPLETED+PARTIAL count from Mongo. Different sets; neither contains the
other. Now: "Lifetime cached (Turso)" (pre-filter gallery:* row count) +
"Cache queue (not yet done/failed)" + "Remaining" = queue size (the old
remaining formula subtracted completed_db from an already-filtered queue —
double-counting, fixed).

v1.27 (2026-09-04): ScraperBot discovery counting + digest window. TWO
root causes for "digest says No new galleries": (1) the per_page_new ring
was only written by details_sweeper/user-hints — list_sweeper (4 sorts x
20 pages + 66 tags x 5 pages) never recorded its discoveries, so the ring
held 10 user-hint entries and zero sort/tag entries (confirmed live in
Mongo). list_sweeper now batch-records true discoveries (gid has no
gallery:<id> row yet — one PK-indexed IN query per page) via the new
channel_dashboard.record_new_galleries_on_page (one Mongo read+write per
page, feeds per_page_new + per_sort + ring24h_by_key + gid dedupe set).
(2) build_report hardcoded a trailing-24h window; it now anchors to the
previous send (discovery_digest_last_sent) so a 10:00 digest covers
everything since the prior send, header reads "(since 03 Sep 10:00 IST)".
New tests: ScraperBot/tests/test_v1_27_discovery.py.

v1.28 (2026-09-05): Turso -> second-Mongo backup service (disaster recovery).
Context: v12.57 brake worked — reads went from ~7M/70min to ~10k/9h after
deploy + Bot 2 suspension, budget safe. But Turso hard-locks at 500M reads;
if that ever fired unattended, all 18,930 cache rows + Bot 2 state would be
unreachable. New app/services/turso_backup.py runs inside ScraperBot
(no new service): every BACKUP_EVERY_HOURS (default 12h) it keyset-paginates
(key > :last, 500-row batches) through nhentai_cache, bot2_fetch_state and
bot2_done and bulk-upserts into a SECOND Mongo cluster (separate account).
Cost: ~19k Turso reads per pass = ~38k/day = 0.2% of budget. Env:
2NDMONGO_BACKUP_TURSO (URI, required; also accepts 2ndmongo_backup_turso /
MONGO2_BACKUP_URI), MONGO2_BACKUP_DB (default turso_backup), BACKUP_ENABLED=0
to idle. Restore: python -m app.services.turso_backup --restore. State +
status in scraper1_state["turso_backup_state"].
