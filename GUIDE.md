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
