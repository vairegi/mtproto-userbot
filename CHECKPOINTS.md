# CHECKPOINTS.md — FixPack (3-bug patch)

Task size: **SMALL** (≤3 file edits + 1 new module) → **50% + 100%** grid.

| %    | Description                                                                 | File-wrapper URL                                     | AI Drive mirror                                                    |
|------|-----------------------------------------------------------------------------|------------------------------------------------------|--------------------------------------------------------------------|
| 50%  | All three bug fixes applied; `py_compile` green on every edited `.py`; JS parses clean; caption smoke-test matches screenshot format exactly. | https://www.genspark.ai/api/files/s/rugh58NV        | `/DoujinshiUniverse_v2_checkpoints/FixPack_50pct.zip`             |
| 100% | `verify_v2.sh` — all 5 stages green (required files, py_compile, grep tripwires, miniapp/verify.sh, tests_v2_smoke.py: 43 PASS). FINAL zip uploaded + mirrored. | *(FINAL URL — see chat)*                             | `/DoujinshiUniverse_v2_checkpoints/FixPack_FINAL.zip`             |

## Files changed

| File | Bug | Purpose of change |
|------|-----|-------------------|
| `miniapp/backend/app/services/scraper_bridge.py`     | BUG 3 | Replaced `asyncio.run()` per call with a persistent per-thread event loop (`threading.local` + `_get_loop()`). Kills the "Event loop is closed" warning from `hf_scraper`'s pooled `httpx.AsyncClient`. |
| `cover_poster.py`                                     | BUG 2 | Rewrote `_format_caption`: strips `(C92)` event prefix + trailing `[English]` brackets; emits `➤ #<gallery_id>`; grouped meta rows (Groups / Parodies / Artists / Characters / Languages / Categories); trailing `➤ Tags:` row capped at 600 chars; **no nhentai URL anywhere**. Reuses existing `_hashtagify`. |
| `miniapp/backend/app/services/dm_delivery.py` *(new)* | BUG 1 | New helper module. `deliver_to_dm(gallery_id, user_id)` reads `db_cover_msg_id` + `db_pdf_msg_id` from the Mongo gallery doc and calls Telegram Bot API `copyMessage` twice with the admin bot token (`copyMessage` strips the "Forwarded from" tag). |
| `miniapp/backend/app/routes/queue.py`                 | BUG 1 | Added `POST /api/queue/deliver/{gallery_id}`. Changed the `already_completed` branch of `POST /api/queue` to call `dm_delivery.deliver_to_dm(...)` and return `{deduped:true, delivered:true, message:"📨 Forwarded to your DM"}` instead of `open_link`. |
| `miniapp/backend/app/config.py`                       | BUG 1 | Added `database_channel_id` setting (from `DATABASE_CHANNEL_ID` / `CHANNEL_ID` env), and made `bot_token` fall back to `ADMIN_BOT_TOKEN`. |
| `miniapp/frontend/js/plugins/card-actions.js`         | BUG 1 | Replaced the `openLink(res.open_link)` branch (both the pre-check `isCompleted` path and the server-dedup response path) with a `POST /api/queue/deliver/<id>` call → `toast("📨 Sent to your DM")` and close the sheet. No more channel jump. |

## Acceptance

- **BUG 1** — user taps Queue on a completed gallery → bot DMs them the cover image + PDF; no channel jump. Fallback: if delivery fails (Bot API refuses, no message IDs stored), the frontend toasts the error and does NOT open the channel link.
- **BUG 2** — cover post in DB channel matches the exact screenshot: bold clean title, `➤ #<id>`, aligned grouped meta rows in order (Groups → Parodies → Artists → Characters → Languages → Categories), then `➤ Tags: …`, no nhentai URL. Smoke-test output verified inline.
- **BUG 3** — `_run_async` now runs on a persistent per-thread loop; `hf_scraper`'s pooled `httpx.AsyncClient` stays bound to a loop that never closes → no more "Event loop is closed" warnings on `/api/gallery/<id>`.

## Verification

- `python3 -m py_compile` — **green** on every edited `.py`.
- Bracket balance — **green** (`card-actions.js` parses under `new Function()`).
- `verify_v2.sh` — **all 5 stages green** (43 assertions passed in `tests_v2_smoke.py`).
