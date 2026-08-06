# CHECKPOINTS — v10 (DoujinshiUniverse V2)

Ledger of every v10 milestone. The file-wrapper https URL is the
authoritative recovery artifact. AI Drive mirror path is provided when
the mirror upload succeeded.

Repo: https://github.com/vairegi/mtproto-userbot  (branch: main)

## v10 milestones (newest first)

| %    | Description                                                                 | File-wrapper URL                                     | AI Drive mirror                                                    |
|------|-----------------------------------------------------------------------------|------------------------------------------------------|--------------------------------------------------------------------|
| 100% | FINAL — v10 done: card-actions.js empty_result handler + ledger; verify_v2.sh 43/43 PASS | (see final chat message)                             | /DoujinshiUniverse_v2_checkpoints/FixPack_v10_100pct.zip           |
| 50%  | card-actions.js empty_result soft-fail handler applied; verify green         | https://www.genspark.ai/api/files/s/xyr16Pfh         | /DoujinshiUniverse_v2_checkpoints/FixPack_v10_50pct.zip            |
| 75%  | v10 anchor — all v10 features shipped except card-actions.js empty_result   | https://www.genspark.ai/api/files/s/Fnayphip         | (pre-existing anchor)                                              |
| 70%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/JmFBwDR0         | —                                                                  |
| 65%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/gn0NUkts         | —                                                                  |
| 60%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/dZfM3VRQ         | —                                                                  |
| 55%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/vgMtXPnG         | —                                                                  |
| 50%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/DeUDSFcs         | —                                                                  |
| 45%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/CpDGHQky         | —                                                                  |
| 40%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/iE3sEFBh         | —                                                                  |
| 35%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/5XwIHlbJ         | —                                                                  |
| 30%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/TU7ZRcB5         | —                                                                  |
| 25%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/h4bZM8fX         | —                                                                  |
| 20%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/uebqMLlr         | —                                                                  |
| 15%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/LK62SenM         | —                                                                  |
| 10%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/WW1VPX46         | —                                                                  |
|  5%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/HnnjOMKu         | —                                                                  |
|  2%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/8xHK5jeW         | —                                                                  |
|  1%  | v10 in-flight checkpoint                                                    | https://www.genspark.ai/api/files/s/ugPrdlsX         | —                                                                  |

## Earlier version FINALs

| Version | File-wrapper URL                                     |
|---------|------------------------------------------------------|
| v9      | https://www.genspark.ai/api/files/s/LEUiosmD         |
| v8      | https://www.genspark.ai/api/files/s/S96BEmlH         |
| v7      | https://www.genspark.ai/api/files/s/ubDQWhKy         |
| v6      | https://www.genspark.ai/api/files/s/Cj3tRDCl         |
| v5      | https://www.genspark.ai/api/files/s/1dMt5gFP         |

## v10 feature scope (recap)

1. ♻️ **Force Re-scrape (admin)** — `services/rescrape.py` + endpoints
   `GET /api/admin/rescrape/failed`, `POST /api/admin/rescrape`,
   `GET /api/admin/rescrape/diag`, admin.js `sectionRescrape()`
   (failed-galleries list with per-row status + `failed_reason`, manual
   URL/ID input).

2. 📣 **Broadcast to Users (admin)** — `services/broadcast.py` +
   `POST /api/admin/broadcast`, `GET /api/admin/broadcast/status/<id>`,
   `GET /api/admin/broadcast/recent`, `GET /api/admin/broadcast/preview`,
   admin.js `sectionBroadcast()` (textarea, optional button, recipient
   preview, live status polling, recent history). Delivery: daemon
   thread at ~20 msg/s; banned users skipped; per-run stats in
   `miniapp_broadcasts`.

3. ⏳ **Live Queue Progress** — `services/progress.py` +
   `GET /api/queue/progress/<gallery_id>`; `queue.js` renders a
   progress card under PROCESSING rows polling every 2.5s
   ("Your PDF is being generated…" + phase detail + optional pct bar,
   auto-cleanup on teardown); `relay_v2._emit_progress` writes 6 phase
   events (scrape 20% → cover 40% → bot2_send 55% → bot2_wait 70% →
   pdf_received 85% → delivered 100%) into `progress_events`.

4. 🎨 **Background themes** — `css/themes.css` (ember default / light
   white / purple nebula), `prefs.js` `background_theme` pref applied
   as `<html data-bg-theme>`, Settings → Appearance "Background"
   selector, server-side app-wide default via
   `GET/POST /api/admin/background` + public
   `GET /api/profile/preferences`, `app.js` boot applies the server
   default ONLY when the user never picked one locally (local explicit
   choice always wins), admin.js `sectionBackground()` selector.

5. ✨ **Stylish buttons** — `css/buttons.css` with 7 pure-CSS variants
   (`btn-glow`, `btn-shine`, `btn-outline-slide`, `btn-lift`,
   `btn-pill`, `btn-ripple`, `btn-icon-only`), theme-aware +
   reduced-motion aware, loaded LAST in `index.html`; `sheet.js` maps
   `kind→variant` (primary=btn-glow+btn-ripple, secondary=btn-lift,
   danger=btn-glow) and supports a per-action `variant` override; all
   legacy buttons upgraded across `admin.js`, `queue.js`, `profile.js`.

6. 🐛 **"Failed: enqueue_batch returned nothing" fix** —
   - backend: `queue.py` now soft-fails with `action="empty_result"` +
     a friendly message (suggests Force Re-scrape) instead of
     `HTTPException(503)` [shipped in v10 75%].
   - frontend: `card-actions.js` `queue_or_open` action's `run()` now
     handles `res.ok === false && res.action === "empty_result"` and
     shows the friendly toast [shipped at v10 50% / 100%].

## Verification (at 100%)

- `bash verify_v2.sh` → `✓ V2 verification passed. Safe to deploy.`
  - Stage 1 syntax: OK
  - Stage 2 py-compile: OK
  - Stage 3 grep tripwires: OK (incl. `card-actions.js has dynamic label`)
  - Stage 4 miniapp inner verify.sh: OK
  - Stage 5 tests_v2_smoke.py: **43 assertions passed**
- `card-actions.js` bracket balance: parens 0 / braces 0 / brackets 0
- `card-actions.js` contains 2 `empty_result` references (comment + handler)

## Deployment note

- Sandbox-only dep gap (production installs from `requirements.txt`):
  `pip install --quiet --no-input pymongo httpx python-telegram-bot telethon python-dotenv`
- Zip layout: top-level `mtproto-userbot/`, excludes `.git/`,
  `__pycache__/`, `*.pyc`.
- Zip metrics: ~305 KB, 132 entries.
