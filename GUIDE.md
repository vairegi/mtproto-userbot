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
