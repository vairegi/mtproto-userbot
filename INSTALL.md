# INSTALL.md — Termius / nano walkthrough for Ryan

> **You have no coding background — that's fine.** Every step below is a
> command you copy-paste into Termius, or a file you paste into `nano`.
> `nano` is a text editor inside the terminal: open with `nano <filename>`,
> paste, save with `Ctrl+O` then `Enter`, exit with `Ctrl+X`.
>
> **Never paste secrets back to the AI.** Real bot tokens, session strings,
> and API hashes belong only in `.env` on your server.

---

## Phase 1 — Env + credentials setup

### 1.1 Log into the server via Termius and make the project folder

Copy-paste this exactly:

```bash
mkdir -p /home/ryan/relay/logs /home/ryan/relay/backups /home/ryan/relay/scripts /home/ryan/relay/pm2
cd /home/ryan/relay
```

### 1.2 Install system prerequisites (one-time, needs sudo)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip sqlite3 nodejs npm
sudo npm install -g pm2
```

### 1.3 Create the Python venv

```bash
cd /home/ryan/relay
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

### 1.4 Upload / paste every project file

Use Termius's built-in file transfer panel to upload the ZIP the AI
gave you, unzip it in `/home/ryan/relay/`, or paste each file with `nano`.
File list you should end up with:

```
/home/ryan/relay/
├── .env.example
├── requirements.txt
├── config.py
├── logging_setup.py
├── db.py
├── url_utils.py
├── queue_service.py
├── startup_check.py
├── userbot.py
├── source_api.py
├── worker.py
├── admin_bot.py
├── scripts/gen_session.py
├── pm2/ecosystem.config.js
├── pm2/logrotate_setup.sh
└── backups/backup.sh
```

### 1.5 Install pinned Python deps

```bash
cd /home/ryan/relay
source .venv/bin/activate
pip install -r requirements.txt
```

### 1.6 Create your `.env`

```bash
cd /home/ryan/relay
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in every `<REDACTED_...>` placeholder. Leave `TELEGRAM_SESSION_STRING`
as-is for now — the next phase generates it. Save with `Ctrl+O`, `Enter`,
`Ctrl+X`.

You will need:

- Telegram **API_ID** and **API_HASH** from https://my.telegram.org
- Your Telegram **phone number** in international format, e.g. `+14155550123`
- An **Admin Bot token** from `@BotFather` (create a brand new bot for this)
- Your own Telegram **user ID** from `@userinfobot`
- Your **Database Channel ID** in the `-100...` form. Easy way:
  forward any message from the channel to `@userinfobot` — it prints the ID.
- Bot 1 username (default `hentaifoxbot`) and Bot 2 username (`Gallery_DLBot`)
- Timezone name, e.g. `Asia/Tokyo`
- Optional: source-site API base URL + key for the fallback (§7). If you
  don't have it yet, leave both blank — the pipeline still runs, it just
  can't self-post covers when Bot 1 misses.

---

## Phase 2 — Userbot login test

### 2.1 Generate the userbot session string (ONCE)

```bash
cd /home/ryan/relay
source .venv/bin/activate
python scripts/gen_session.py
```

- Telegram will send a login code to your account. Type it in.
- If you have 2-factor auth, type that password too.
- The script prints one very long line — that's your session string.

### 2.2 Paste the session string into `.env`

```bash
nano .env
```

Replace `<REDACTED_SESSION_STRING>` with the string. Save and exit.
**Do not paste this string to anyone, including the AI assistant.**

### 2.3 Sanity check — startup self-test

```bash
cd /home/ryan/relay
source .venv/bin/activate
python startup_check.py
```

You want to see four `[OK]` lines and the process exiting with code 0.
If any line is `[FAIL]`, fix the reason it shows and rerun before continuing.

### 2.4 Send `/start` to both bots (manually)

Open your Telegram app. Send `/start` to `@hentaifoxbot` and to
`@Gallery_DLBot` — some bots refuse to DM you unless you initiated first.

---

## Phase 3 — SQLite queue + `/fetch` parsing (no relay yet)

### 3.1 Start the Admin Bot in the foreground for testing

```bash
cd /home/ryan/relay
source .venv/bin/activate
python admin_bot.py
```

In Telegram, DM your Admin Bot:

- `/fetch` — should reply with the one-line usage hint.
- `/fetch https://example.com/gallery/123` — should reply
  `1 queued, 0 rejected, 0 skipped as duplicates`.
- Send the same URL again — should say `0 queued, 0 rejected, 1 skipped as duplicates`.
- Send `/queue` — should show `pending: 1`.

Stop the bot with `Ctrl+C` when done.

---

## Phase 4 — Full relay loop on ONE real link

### 4.1 Start both processes in the foreground, in two Termius tabs

Tab A (worker):

```bash
cd /home/ryan/relay
source .venv/bin/activate
python worker.py
```

Tab B (admin):

```bash
cd /home/ryan/relay
source .venv/bin/activate
python admin_bot.py
```

DM the Admin Bot: `/fetch <one real gallery URL>`.
Watch the Database Channel — you should see Bot 1's cover post, then the
PDF forwarded right underneath.

Verify:

- `/status` shows the job as `DONE`.
- `/health` shows `disk free`, `paused: no`, non-empty last-DM timestamps.

Stop both with `Ctrl+C`.

---

## Phase 5 — pm2 (auto-restart + reboot survival) + log rotation

### 5.1 Start under pm2

```bash
cd /home/ryan/relay
pm2 start pm2/ecosystem.config.js
pm2 save
pm2 startup
```

`pm2 startup` prints a `sudo env ...` command. Copy that command exactly
and run it once — that's what makes pm2 come back after a server reboot.

### 5.2 Install pm2-logrotate

```bash
bash /home/ryan/relay/pm2/logrotate_setup.sh
pm2 save
```

### 5.3 Handy pm2 commands (bookmark these)

```bash
pm2 status                     # list both processes and their state
pm2 logs relay-worker          # live logs
pm2 logs relay-admin
pm2 restart relay-worker
pm2 restart relay-admin
pm2 stop all
pm2 start all
```

---

## Phase 6 — Batch test (3–4 real links)

DM the Admin Bot with:

```
/fetch
https://example.com/gallery/aaa
https://example.com/gallery/bbb
https://example.com/gallery/ccc
```

Watch the Database Channel — you should see them appear one by one with
a 20–60s gap between jobs. When the queue drains, the Admin Bot DMs you a
batch summary. `/status` shows the last five jobs.

---

## Phase 7 — Nightly backup cron

### 7.1 Confirm the script works standalone

```bash
bash /home/ryan/relay/backups/backup.sh
ls -lh /home/ryan/relay/backups/
```

You should see a `queue.db.bak.<today>` file.

### 7.2 Install the crontab entry

```bash
crontab -e
```

Add this line **exactly** (runs 03:15 daily in server time):

```
15 3 * * * /bin/bash /home/ryan/relay/backups/backup.sh >> /home/ryan/relay/logs/backup.log 2>&1
```

Save and exit. Verify with `crontab -l`.

---

## Everyday cheat sheet

| I want to... | Do this |
|---|---|
| Queue new links | DM `/fetch <urls>` to your Admin Bot |
| See what's happening | `/status` |
| See why the last job failed | `/last` |
| Overall system check | `/health` |
| Pause everything | `/pause` |
| Resume | `/resume` |
| Read live server logs | `pm2 logs relay-worker` |
| Restart after a code change | `pm2 restart all` |

---

## Troubleshooting

**"userbot session not authorised" on startup**
Your session string expired or got revoked. Re-run
`python scripts/gen_session.py` and update `.env`.

**Jobs sit at `processing` forever**
Kill the worker (`pm2 restart relay-worker`). On next start, the worker
resets any stuck `processing` row back to `pending` automatically.

**Admin Bot doesn't reply at all**
That's the security feature working (§10). Only messages from your own
user ID get any response. Confirm `ADMIN_USER_ID` in `.env` matches your
Telegram numeric ID.

**Database locked errors in logs**
Should not happen — WAL + `busy_timeout=5000` is set. If it does, do NOT
run a second worker process. Only one worker at a time.

**Disk filling up**
Check `pm2 logs` weren't disabled. Rerun the logrotate installer:
`bash /home/ryan/relay/pm2/logrotate_setup.sh`.
