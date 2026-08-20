---
title: MTProto Userbot Relay
emoji: 🤖
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
suggested_hardware: cpu-basic
---

# MTProto Userbot Relay

Telegram userbot + admin bot pipeline, running on Hugging Face Spaces.

## Required Secrets

Add these under **Settings → Variables and secrets** as **Secrets** (not variables):

| Name | What it is |
|------|------------|
| `API_ID` | Telegram API ID (from https://my.telegram.org) |
| `API_HASH` | Telegram API hash |
| `STRING_SESSION` | Telethon StringSession for the user account |
| `BOT_TOKEN` | Admin bot token from @BotFather |
| `MONGO_URI` | MongoDB Atlas connection string (`mongodb+srv://...`) |
| `ADMIN_USER_ID` | Your own Telegram numeric user id |
| `BOT1_USERNAME` | Bot 1 username (no @) |
| `BOT2_USERNAME` | Bot 2 username (no @) |
| `DATABASE_CHANNEL_ID` | Numeric id of the Database Channel |

Optional: `DOUJINSHIBOT_USERNAME`, `SOURCE_API_BASE`, `SOURCE_API_KEY`, `LOG_LEVEL`, `TIMEZONE`.

The container starts `start.sh`, which brings up `admin_bot.py` and `worker.py`
in the background and the Mini App (uvicorn) in the foreground.
