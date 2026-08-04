# Environment variable reference (V2)

This file describes every env var the running project reads, why it exists,
what happens if you leave it unset, and how it interacts with the V2
architecture (`docs/ARCHITECTURE_V2.md`).

For a ready-to-copy template, see [`.env.example`](../.env.example) at
the repo root.

---

## Required at all times

| Variable | Purpose | Notes |
| --- | --- | --- |
| `API_ID`, `API_HASH` | Telethon userbot session credentials | From https://my.telegram.org |
| `STRING_SESSION` | Userbot auth (no phone prompt at boot) | Generate once with `scripts/gen_session.py` |
| `BOT_TOKEN` | admin_bot.py token | From @BotFather |
| `ADMIN_USER_ID` | Numeric Telegram user ID of the owner | e.g. `8679252317` |
| `MONGO_URI` | MongoDB Atlas connection string | Alias: `MONGODB_URI` |
| `DATABASE_CHANNEL_ID` | Channel that stores covers + PDFs | `-100…` form |
| `BOT2_USERNAME` | @Gallery_DLBot (or fork) | No `@` prefix required |

## V2 additions (⚡)

| Variable | Default | Purpose |
| --- | --- | --- |
| `SELF_COVER_POST_ENABLED` | `1` | Master V2 switch. Set `0` to fall back to legacy Bot 1 path. |
| `MINIAPP_STALE_PROCESSING_S` | `900` | Lazy timeout (seconds) for stuck PROCESSING gallery docs. |

## V2 deprecations

| Variable | V2 state | Behaviour |
| --- | --- | --- |
| `BOT1_USERNAME` | Deprecated but tolerated | Logs a `WARNING` at boot if set; ignored unless `SELF_COVER_POST_ENABLED=0`. |
| `BOT1_POST_TIMEOUT_SEC` | Deprecated | No longer consulted by the V2 path; still accepted so V1 rollback works. |

## Mini App

| Variable | Default | Purpose |
| --- | --- | --- |
| `MINIAPP_URL` | (empty) | Public URL of the Mini App, used by `/app` and the V2 `/search` redirect. |
| `MINIAPP_LOG_LEVEL` | `INFO` | Log level for the FastAPI process. |
| `MINIAPP_PUBLIC_DEFAULT` | `1` | Initial public/private toggle on first boot. |
| `MINIAPP_DEFAULT_DAILY_LIMIT` | `20` | Per-user daily queue quota. Runtime-adjustable via the Admin tab. |
| `MINIAPP_DEFAULT_COOLDOWN_S` | `0` | Per-user cooldown between queues. |

## Optional

| Variable | Purpose |
| --- | --- |
| `DOUJINSHIBOT_USERNAME` | `/mpost` target. Default `Douginshibot`. |
| `SOURCE_API_BASE`, `SOURCE_API_KEY` | Fallback metadata provider. Leave empty to disable. |
| `TIMEZONE` | Default `UTC`. Used by the auto-queue scheduler. |
| `INTER_JOB_DELAY_MIN`, `INTER_JOB_DELAY_MAX` | Bounds (in seconds) between successive jobs. |
| `BATCH_MAX_LINKS` | Max URLs accepted in a single bot message. |
| `AUTO_FETCH_DOMAINS` | Comma-separated allow-list of gallery domains. |

---

## Recommended Render settings for V2

Copy these values into Render's environment UI when upgrading to V2:

```
SELF_COVER_POST_ENABLED     = 1
MINIAPP_STALE_PROCESSING_S  = 900
BOT2_PDF_TIMEOUT_SEC        = 480
MINIAPP_PUBLIC_DEFAULT      = 1
MINIAPP_DEFAULT_DAILY_LIMIT = 20
MINIAPP_DEFAULT_COOLDOWN_S  = 0
MINIAPP_LOG_LEVEL           = INFO
```

Then REMOVE `BOT1_USERNAME` from the env once you've confirmed the V2 path
works. Rolling back is `SELF_COVER_POST_ENABLED=0` — you don't need to
restore `BOT1_USERNAME` unless you plan to run the legacy path for more
than the length of the current session.
