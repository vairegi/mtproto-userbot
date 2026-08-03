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
