# Integration Guide

How to wire the Mini App into your existing `admin_bot.py` project on Render.

## 1. File layout after integration

Drop the `miniapp/` folder next to your existing bot files:

```
mtproto-userbot/
├── admin_bot.py
├── hf_scraper.py
├── db.py
├── queue_service.py
├── start.sh
├── ...
└── miniapp/                     ← this project
    ├── backend/
    ├── frontend/
    └── docs/
```

The Mini App backend adds `/opt/render/project/src` (and a couple of relative candidates) to `sys.path` at boot, so `import hf_scraper`, `import queue_service`, and `import db` all resolve to the SAME modules the bot uses. That means: jobs queued via the Mini App go into the SAME MongoDB queue `worker.py` polls. No duplication, no drift.

## 2. Environment variables (add to Render)

You already have these from the bot:

```
BOT_TOKEN
ADMIN_USER_ID
MONGO_URI
```

Add these new optional ones:

```
MINIAPP_PUBLIC_DEFAULT      = 1     # 1 = open to all on first boot; admin can flip
MINIAPP_DEFAULT_DAILY_LIMIT = 20    # per-user daily queue quota
MINIAPP_DEFAULT_COOLDOWN_S  = 0     # seconds between queues (0 = off)
MINIAPP_LOG_LEVEL           = INFO
MONGO_DB                    = doujinshi   # or whatever DB name you already use
```

Everything else the admin can control at runtime from the in-app **Admin → Rate Limits** panel — nothing needs to be redeployed to adjust limits.

## 3. Replace the dummy HTTP server in start.sh

Your current start.sh runs `python3 -m http.server $PORT & bash start.sh` to pass Render's port scan. The Mini App backend replaces that — it serves the frontend AND passes the port scan on the same port.

Change the top of `start.sh` from:

```bash
python3 -m http.server $PORT &
```

to:

```bash
# --- Mini App: serves the frontend AND passes Render's port scan ---
cd "$(dirname "$0")"
uvicorn miniapp.backend.main:app \
    --host 0.0.0.0 \
    --port "$PORT" \
    --log-level info &
MINIAPP_PID=$!
echo "[start.sh] miniapp started, pid=$MINIAPP_PID on port $PORT"
```

Also install the extra requirements once at build:

```bash
pip install -r miniapp/backend/requirements.txt
```

You can `cat` that file onto the end of your existing top-level `requirements.txt` if you prefer a single install step.

## 4. Register the Mini App with BotFather

In Telegram, open **@BotFather**:

```
/mybots  →  <your bot>  →  Bot Settings  →  Configure Mini App
    →  Edit Mini App URL
    →  https://<your-render-service>.onrender.com/
```

That's it. The `/` route serves `index.html`. Telegram opens it in the built-in webview whenever the user taps a Web App button.

Optional — add a launcher command:

```
/setmenubutton  →  <your bot>  →  set button text: "🌌 Universe"
                                  set URL:         https://<your-render-service>.onrender.com/
```

Now every chat with your bot has a persistent "🌌 Universe" button that opens the Mini App.

## 5. Add a launcher inside admin_bot.py (optional but nice)

Add a `/app` command that gives users a Web App button, so they don't have to hunt for the menu button:

```python
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import CommandHandler, ContextTypes

MINIAPP_URL = "https://<your-render-service>.onrender.com/"

async def cmd_app(update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🌌 Open Universe", web_app=WebAppInfo(url=MINIAPP_URL))
    ]])
    await update.effective_message.reply_text(
        "Tap to open the browser:", reply_markup=kb
    )

# In build_app():
application.add_handler(CommandHandler("app", cmd_app))
```

## 6. Verify

```
curl https://<your-render-service>.onrender.com/healthz
    → {"ok": true, "service": "miniapp"}

curl https://<your-render-service>.onrender.com/
    → the index.html HTML shell
```

Then open the bot in Telegram, tap the menu button / `/app`, and you should see the Doujinshi Universe app render inside Telegram.

## 7. Rollback

Everything the Mini App writes is in `miniapp_*`-prefixed Mongo collections. Nothing touches your existing collections. To fully remove:

1. Revert start.sh
2. Delete the `miniapp/` folder
3. (Optional) drop `miniapp_settings`, `miniapp_users`, `miniapp_usage`, `miniapp_bookmarks` in Atlas

The bot itself is unaffected.

---

# V2 addendum (Bot 1 removed, MongoDB `galleries` dedup gate)

If you are upgrading to V2, read `docs/ARCHITECTURE_V2.md` (repo root) and
`docs/MIGRATION_V2.md` first. This addendum covers only the mini-app-side
changes and the operational steps the operator has to take on Render.

## V2-a. New environment variables

Add these to your Render service alongside the existing miniapp vars:

```
SELF_COVER_POST_ENABLED     = 1     # 0 = fall back to legacy V1 (Bot 1) path
MINIAPP_STALE_PROCESSING_S  = 900   # 15 minutes — dedup gate lazy-timeout
```

Deprecated, still tolerated:

```
BOT1_USERNAME               = <unset>   # V2 logs a warning if set; ignored
```

Unchanged:

```
BOT2_USERNAME               = @Gallery_DLBot
BOT2_PDF_TIMEOUT_SEC        = 480   # 8-minute Bot 2 wait
```

## V2-b. New backend endpoints & response shapes

Route | Change
--- | ---
`POST /api/queue` | Dedup gate runs BEFORE the rate-limit consume. Response can now be `{deduped:true, action:"already_completed", open_link, title, ...}` / `{deduped:true, action:"already_processing", ...}` / `{deduped:false, action:"queued", job, usage}`.
`GET /api/gallery/{id}` | Response now carries `v2_status: {known, status, open_link, title, ...}` (read-only) so the sheet renders the correct primary button in one round-trip.
`GET /api/gallery/{id}/status` | **NEW** read-only endpoint returning `{gallery_id, known, status, open_link, title, pages, completed_at, failed_reason}`. Front-end uses it to swap "Queue" ⇄ "Open Post" on cards.
`GET /api/queue/status` | `recent[]` rows are enriched with `open_link`, `gallery_id`, `error_reason` so the Queue tab can render "Open Post" without extra requests.

## V2-c. New client behaviour

- **Card action swap**: `frontend/js/plugins/card-actions.js` — the primary
  action now has function-valued `label` / `icon` / `disabled` fields.
  Detail-sheet consumers (`pages/search.js`, `pages/bookmarks.js`) unwrap
  them and pre-fetch `/api/gallery/{id}/status` on sheet open. This is
  invisible to end-users but critical if you add third-party card actions.
- **Queue tab**: `frontend/js/pages/queue.js` — done / partial rows render
  an "🔗 Open Post" button that opens the DB channel deep-link via Telegram
  WebApp's `openLink`. Failed rows show a friendly reason (technical text
  stays admin-only).
- **`/search` command** in `admin_bot.py` now redirects to the Mini App
  with an inline `WebAppInfo` button, pre-filling the query in the URL
  hash. The legacy `search_picker` module remains wired up so callback
  handlers for in-flight legacy sessions still work.

## V2-d. One-time migration

After V2 goes live, run once (from the Render shell, or locally with the
production `MONGO_URI`):

```
python3 scripts/migrate_v2_recover_stuck.py --dry-run
python3 scripts/migrate_v2_recover_stuck.py
```

This tombstones orphaned `PROCESSING` gallery docs (e.g. from a container
recycle during V2 first deploy) as `FAILED_RECOVERED`, which the dedup
gate treats as retryable. See the script's docstring for details.

## V2-e. Verification

- Enqueue a brand-new gallery from the Mini App → cover appears in DB
  channel authored by the userbot session (not Bot 1) → PDF forwarded
  under it → the card / detail sheet immediately shows "🔗 Open Post".
- Enqueue the same gallery from a different Telegram account → response
  is `{deduped:true, action:"already_completed"}` → user is bounced
  straight to the existing DB channel post. No new post, no token spent.
- Enqueue during processing from another account → response is
  `{deduped:true, action:"already_processing"}` → user sees "already
  downloading — hang tight" toast.
- Send a link Bot 2 can't handle → cover post is deleted, admin gets a
  DM with the offending link + Bot 2's raw error text, user sees
  "source error — please pick another gallery" in their chat.
- Trigger a stale PROCESSING doc (kill worker mid-flight) and run the
  migration script → doc becomes FAILED_RECOVERED; next dedup request
  for that gallery_id proceeds.
- Open the mini-app Queue tab → COMPLETED rows show "🔗 Open Post".

## V2-f. Rollback

Set `SELF_COVER_POST_ENABLED=0` on Render, redeploy. The router in
`worker.py` routes back to legacy `relay.process_job` (which still expects
`BOT1_USERNAME`). The `galleries` collection stays in Mongo but is not
consulted by the V1 path; it is safe to leave it there or drop it.
