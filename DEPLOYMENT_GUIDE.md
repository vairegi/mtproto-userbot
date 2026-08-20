# Deployment Guide — MTProto Userbot on MongoDB Atlas + Hugging Face Spaces

Read this end-to-end **before touching anything**. Every step is written for
someone who has never used MongoDB, Docker, or Hugging Face before. Copy /
paste commands and values exactly as shown — do not type them from memory.

---

## Part A — Set up MongoDB Atlas (the free cloud database)

This gives you the `MONGO_URI` value you will need in Part B.
Time required: about 10 minutes.

### A1. Create your Atlas account

1. Open <https://www.mongodb.com/cloud/atlas/register> in your browser.
2. Sign up with your email (or "Sign in with Google" — faster).
3. When Atlas asks screening questions ("What language will you use?" etc.),
   pick anything. Nothing on that page changes what you get.

### A2. Create a free cluster

1. Once logged in, click **Build a Database** (or **Create → Cluster**).
2. Choose the **M0 FREE** tier — this is the box that says **$0 / Forever free**.
3. Provider: leave **AWS** selected.
4. Region: pick the one geographically closest to where Hugging Face runs
   (usually **N. Virginia (us-east-1)** works fine).
5. Cluster name: leave the default `Cluster0` — do not rename it.
6. Click **Create Deployment**. Wait 1–3 minutes while Atlas provisions.

### A3. Create the database user (this is the account your bot logs in as)

Atlas will show a "Connect to Cluster0" popup as soon as the cluster is ready.
If it does not, click **Database** in the left sidebar → **Connect**.

1. Under **Create a database user**:
   - Username: `relaybot`
   - Password: click **Autogenerate Secure Password** → then **Copy** the
     password to a text file on your computer. You need it in step A6.
2. Click **Create Database User**.

> ⚠ Very important: the password may contain characters like `@`, `#`, `%`,
> `/`, `:`. If it does, you MUST URL-encode them later (Atlas gives you a
> pre-encoded URI in step A6, so just use that — do not retype the password).

### A4. Allow the internet to connect (Network Access)

1. In the left sidebar click **Network Access** → **Add IP Address**.
2. Click **ALLOW ACCESS FROM ANYWHERE**. The IP field auto-fills with
   `0.0.0.0/0`.
3. Click **Confirm**.

> Why this is needed: Hugging Face Spaces containers do not have a fixed IP,
> so we cannot allow-list them specifically. The database is still protected —
> anyone connecting still needs the username + password from A3.

### A5. Wait for both green checks

- Left sidebar → **Database** → your cluster should show status **Active**.
- Left sidebar → **Network Access** → status should show **Active**.

If either says "Pending", wait 1–2 minutes and refresh.

### A6. Copy your connection string (this becomes MONGO_URI)

1. Left sidebar → **Database** → next to Cluster0, click **Connect**.
2. Choose **Drivers** (the middle option, "Connect your application").
3. Driver: **Python**. Version: whatever is latest (e.g. 4.11 or later).
4. Atlas shows a long string that looks like this:

   ```
   mongodb+srv://relaybot:<db_password>@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0
   ```

5. **Manually replace** the literal text `<db_password>` (including the angle
   brackets) with the password you copied in step A3.

   Example — if your password is `Xy7q!Zn3PbR2`, the result is:

   ```
   mongodb+srv://relaybot:Xy7q!Zn3PbR2@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0
   ```

6. Save the finished string in the same text file where you kept the
   password. This is your **MONGO_URI**. You will paste it into Hugging Face
   in Part B.

> If your password contains special characters (`@`, `#`, `/`, `:`, `?`, `%`,
> space), open <https://www.urlencoder.org/>, encode the password only, and
> paste the encoded version into the URI. `@` becomes `%40`, `#` becomes
> `%23`, and so on.

### A7. Nothing else in Atlas needs configuring

You do NOT need to create a database, create collections, run any commands,
or "seed" any data. The bot creates every collection it needs on first run.

---

## Part B — Deploy on Hugging Face Spaces (free 24/7 hosting)

Time required: about 15 minutes.

### B1. Create your Hugging Face account

1. Open <https://huggingface.co/join>.
2. Sign up with email or Google. Verify the email link they send.

### B2. Get your Telegram STRING_SESSION (one-time, on your own computer)

This is the encrypted login token that lets the bot use YOUR user account.

1. On your own computer (not Hugging Face), install Python 3.11+ if you don't
   have it: <https://www.python.org/downloads/>
2. Open Terminal / Command Prompt and run:

   ```bash
   pip install telethon
   ```

3. Save this as `gen_session.py` on your Desktop:

   ```python
   from telethon.sync import TelegramClient
   from telethon.sessions import StringSession
   api_id   = int(input("API_ID: "))
   api_hash = input("API_HASH: ").strip()
   with TelegramClient(StringSession(), api_id, api_hash) as c:
       print("\nYour STRING_SESSION (copy the WHOLE line):\n")
       print(c.session.save())
   ```

4. Get your API_ID and API_HASH from <https://my.telegram.org> → API
   development tools → create a new app.

5. Run `python gen_session.py`, paste the API_ID and API_HASH, log in with
   your phone number and the code Telegram sends you. It prints a long
   string starting with `1BQAN...` — that is your **STRING_SESSION**. Save it
   to your notes file.

> The project already includes a `scripts/gen_session.py` doing the same
> thing — use whichever is easier.

### B3. Create your Hugging Face Space

1. Go to <https://huggingface.co/new-space>.
2. **Owner**: your username.
3. **Space name**: anything you like, e.g. `relay-bot` (no spaces).
4. **License**: `mit` is fine.
5. **Select the Space SDK**: click the **Docker** card. **Not** Streamlit,
   **not** Gradio.
6. **Docker template**: **Blank**.
7. **Hardware**: keep **CPU basic - Free**.
8. **Visibility**: **Private** is strongly recommended (your bot's code is
   otherwise visible to everyone on the internet).
9. Click **Create Space**.

### B4. Add your Secrets

The bot needs 9 secrets. Add them BEFORE you upload the code — otherwise the
Space starts, fails to find the secrets, and shuts itself down within a
minute.

1. In your new Space, click the **Settings** tab (top right).
2. Scroll to **Variables and secrets**.
3. For each row in the table below, click **New secret**, enter the Name
   exactly as shown (case matters), paste the Value, then click **Save**.

   | Name                  | Value |
   |-----------------------|-------|
   | `API_ID`              | The number from my.telegram.org |
   | `API_HASH`            | The hex string from my.telegram.org |
   | `STRING_SESSION`      | The long `1BQAN...` string from B2 |
   | `BOT_TOKEN`           | The Admin Bot token from @BotFather |
   | `ADMIN_USER_ID`       | Your own Telegram user id (get it from @userinfobot) |
   | `MONGO_URI`           | The full connection string from step A6 |
   | `BOT1_USERNAME`       | e.g. `hentaifoxbot` (no @) |
   | `BOT2_USERNAME`       | e.g. `Gallery_DLBot` (no @) |
   | `DATABASE_CHANNEL_ID` | The numeric id of your Database Channel (e.g. `-1001234567890`) |

   Optional extras (only if you use them):
   `DOUJINSHIBOT_USERNAME`, `SOURCE_API_BASE`, `SOURCE_API_KEY`, `LOG_LEVEL`, `TIMEZONE`.

> All 9 go under **Secrets**, not **Variables**. Secrets are hidden from the
> logs and from anyone who visits your Space.

### B5. Upload the project files

There are two ways. Pick whichever feels easier.

**Option 1 — Web upload (easiest, no software needed)**

1. In your Space, click the **Files** tab (top).
2. Click **Add file → Upload files**.
3. Drag the entire contents of the updated project folder (NOT the outer
   `project/` folder itself — its **contents**) into the drop zone. That
   means these files at the top level:

   ```
   admin_bot.py          config.py           db.py
   Dockerfile            .env.example        .gitignore
   hf_scraper.py         INSTALL.md          logging_setup.py
   progress_tracker.py   queue_service.py    README.md
   README_HF.md          requirements.txt
   scripts/              search_picker.py    source_api.py
   start.sh              startup_check.py    tests_db_mongo.py
   url_utils.py          userbot.py          worker.py
   DEPLOYMENT_GUIDE.md
   ```

4. Scroll down and click **Commit changes to main**.

> **Important:** Do NOT upload `.env` (the file with real secrets) or
> `queue.db` (the old database). The included `.gitignore` prevents this
> automatically if you use git, but on the web upload it's up to you. If
> either file exists in the ZIP, delete it before uploading.

**Option 2 — Git push (faster for future updates)**

```bash
git clone https://huggingface.co/spaces/YOUR_USERNAME/relay-bot
cd relay-bot
# copy every file from the updated project into this folder
git add .
git commit -m "Initial MongoDB deployment"
git push
```

### B6. Copy README_HF.md's header to README.md

Hugging Face reads YAML config from the very top of `README.md` (not from
`README_HF.md`). Open `README.md` in the Files tab, click the pencil icon,
and paste this block at the very top:

```
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
```

Save the commit. This is what tells Hugging Face to build your `Dockerfile`.

### B7. Watch the build

1. Click the **Logs** tab (top). You'll see 3 phases in order:

   - **Building**: Docker builds your image. Takes 3–8 minutes on first
     build. When done, it says `Pushing image...`.
   - **Running**: The container starts. In the log you will see, in order:
     ```
     [start.sh ...] env check: all required variables are present
     [start.sh ...] MongoDB connection verified
     [start.sh ...] launching background processes
     userbot.py: session OK — logged in as @yourhandle [id=...]
     ```
   - **Idle → Running (green dot)** at the top of the page: the Space is
     live.

2. Send `/status` to your Admin Bot on Telegram. If it replies, everything
   works. 🎉

### B8. Keeping it awake

Free Hugging Face Spaces pause after ~48 hours of no HTTP requests. Because
this is a bot (no HTTP traffic), the timer would eventually fire. The
included Dockerfile exposes port 7860 with a health endpoint, and
Hugging Face's own dashboard traffic keeps the container running in most
setups. If you find your Space sleeping, use a free uptime checker like
<https://uptimerobot.com/> to ping your Space's URL every 15 minutes.

---

## Common problems and fixes

**Log says "MongoDB FAILED: ServerSelectionTimeoutError"**
→ Network Access in Atlas is not set to `0.0.0.0/0`. Redo step A4.

**Log says "authentication failed"**
→ The password inside `MONGO_URI` is wrong or contains special characters
that need URL-encoding. Redo step A6, or use Atlas's own "Copy" button which
already encodes correctly.

**Log says "userbot.py: FATAL — the session string is NOT authorised"**
→ Your `STRING_SESSION` was revoked (someone logged you out) or was
truncated when pasted. Regenerate it (step B2) and update the secret in
Settings → Variables and secrets.

**Log says "Missing required environment variable: X"**
→ You forgot to add secret X, or you added it as a Variable instead of a
Secret, or the name has a typo (they are case-sensitive).

**Space shows red "Runtime error" but no logs**
→ Almost always means the Dockerfile failed to build. Click **Logs → Build**
(not Runtime) to see the pip / apt error.

**How do I update the bot later?**
→ Edit any file in the Files tab and commit — the Space auto-rebuilds. Or,
if you cloned with git, edit locally and `git push`.

**How do I stop the bot temporarily?**
→ Settings → **Pause this space**. Restart with the same button.

**Do I need to back up MongoDB?**
→ Atlas M0 does automatic daily snapshots for 2 days. For longer retention
either upgrade to M2 (~$9/mo) or export weekly with `mongodump` from your
own computer.
