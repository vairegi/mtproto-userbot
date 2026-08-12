# Bot 2 — Scraper + Worker (DoujinshiUniverse v12.19 split)

Runs: worker.py (Telethon userbot + relay_v2) + all background crons
(prefetch_cron, details_prefetch_cron, dedup_cron, deletion_scheduler).
Never runs: the FastAPI mini-app or admin_bot.py.

## Deploy (Render account 2 — a SECOND, separate account)
1. New GitHub repo e.g. `doujinshi-bot2`. Drag ALL files from this folder into the repo root.
2. Render → New → **Background Worker** (NOT Web Service — worker.py has no HTTP port).
3. Instance: 512 MB.
4. Copy every var from `.env.example` into Render's Environment panel.
   MONGO_URI / MONGO_DBNAME / TURSO_URL / TURSO_TOKEN / DATABASE_CHANNEL_ID
   must be EXACTLY the same values as Bot 1.
5. Deploy. Good logs: startup_check passes, worker connects as your userbot,
   prefetch sweep lines appear within ~5 min.

## What Bot 2 does
- Polls Mongo for PENDING queue jobs (queued by Bot 1 users).
- Userbot sends the link to @Gallery_DLBot, receives the PDF, uploads it to the
  DB channel under the cover post, marks the Mongo record COMPLETED.
- Crons keep Turso warm: list pages (search:<sort>:page<N>, 20 pages × 4 sorts),
  gallery details, dedup, deletion scheduling.
- v12.18 RAM safeguards included: flood_sleep_threshold=300, request_retries=3.

## Verify before deploy
`bash verify_bot2.sh` → must print "Bot 2 verification PASSED".
