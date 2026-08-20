# ScraperBot (BOT 1)

Isolated background scraper that hydrates the SAME MongoDB + Turso cache
BOT 0 reads from, so the mini-app serves details instantly without BOT 0
ever having to scrape.

- Runs as a **FastAPI web service** (Render Free) with UptimeRobot keeping it warm.
- **Reads/writes only cache keys**: `search:<sort>:page<N>` and `gallery:<id>`.
- **Never touches** BOT 0's queue, users, admins, or `galleries` state-machine docs.
- Zero shared code with BOT 0 — same folder, own dependencies, own process.

## Endpoints

| Path              | Purpose                                       |
| ----------------- | --------------------------------------------- |
| `GET  /`          | Bot info + live status (UptimeRobot pings here) |
| `GET  /healthz`   | Health probe                                  |
| `GET  /status`    | Sweep counters, last-run timestamps           |
| `POST /trigger`   | Force one sweep now (admin key)               |
| `POST /pause`     | Pause both sweepers (admin key)               |
| `POST /resume`    | Resume both sweepers (admin key)              |
| `POST /telegram`  | Telegram webhook — `/status`, `/pause`, `/resume`, `/trigger` in chat |

## Files

```
ScraperBot/
├── app/
│   ├── __init__.py
│   ├── config.py               # env parsing
│   ├── logging_setup.py
│   ├── mongo_client.py         # Mongo handle (shared cluster)
│   ├── turso_client.py         # libsql client (shared DB)
│   ├── cache.py                # cache-key helpers + token bucket (matches BOT 0)
│   ├── hf_scraper_lite.py      # nhentai API client (list + detail)
│   ├── auth.py                 # admin key + Telegram user-id gate
│   ├── services/
│   │   ├── __init__.py
│   │   ├── list_sweeper.py     # search:<sort>:page<N> warmer
│   │   ├── details_sweeper.py  # gallery:<id> warmer
│   │   └── telegram_bot.py     # thin bot API wrapper (webhook)
│   └── routes/
│       ├── __init__.py
│       ├── health.py
│       ├── status.py
│       ├── admin.py
│       └── telegram.py
├── scripts/
│   └── set_webhook.py          # one-shot webhook registration helper
├── main.py                     # FastAPI entrypoint + startup tasks
├── requirements.txt
├── Procfile
├── runtime.txt
├── .env.example
└── README.md
```

See `DEPLOY.md` in the parent chat for the step-by-step Render deploy guide.
