# Telegram Gallery-Fetch Relay Pipeline

Implementation of Build Brief v2 for Ryan.

**What this does:** Ryan sends `/fetch <url>` (one URL per line) to an Admin Bot.
For each URL the userbot DMs `@hentaifoxbot` (Bot 1) and `@Gallery_DLBot` (Bot 2),
waits for Bot 1's cover post to appear in the Database Channel, then natively
forwards Bot 2's PDF right underneath it — one link at a time, no downloads,
no re-uploads.

**Runtime target:** Ubuntu 24.04, Python 3.11/3.12, no headless browsers, pm2.

**Layout on the server:**

```
/home/ryan/relay/
├── .env                    # secrets (chmod 600) — Ryan creates from .env.example
├── .env.example
├── requirements.txt        # pinned
├── config.py
├── logging_setup.py
├── db.py
├── url_utils.py
├── queue_service.py
├── startup_check.py
├── userbot.py              # Telethon client (long-running worker)
├── relay.py                # per-job flow: DM → wait → match → forward
├── worker.py               # main loop entry point
├── admin_bot.py            # separate process, python-telegram-bot
├── queue.db                # created on first run
├── logs/                   # created on first run
├── backups/
│   └── backup.sh
├── pm2/
│   └── ecosystem.config.js
└── INSTALL.md              # Ryan's step-by-step guide
```

Two pm2 processes: `relay-worker` (userbot) and `relay-admin` (admin bot).
They share the same SQLite file but never write in parallel — the admin bot
only inserts into `queue`; the worker owns everything else.

See `INSTALL.md` for the full walkthrough written for Termius/nano.
