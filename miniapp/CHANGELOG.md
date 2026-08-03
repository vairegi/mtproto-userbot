# Changelog

All notable additions to Doujinshi Universe Mini App. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).

## [0.1.0] — 2026-08-03 — Initial scaffold

### Frontend
- Telegram Mini App shell (`index.html`) with import-map ES module boot
- Design token layer (`css/theme.css`) — every color/radius/spacing in ONE place
- Component library: `card`, `chip`, `sheet`, `toast`, `skeleton` — all registered in a swappable factory
- Pages: `search`, `bookmarks`, `queue`, `profile`, `settings`, `admin` — each in its own file, registered via `core/registry.js`
- Plugins: `card-actions.js`, `search-operators.js`, `preview-modal.js` — one-file feature toggles
- Core infra: `state.js` (reactive store), `api.js` (fetch wrapper), `telegram.js` (SDK wrapper), `back-stack.js` (native back-button routing), `prefs.js` (localStorage user prefs)
- Native Telegram theme sync + haptic feedback + back-button integration
- Live queue-status badge in header (5s poll)
- Profile page matching the "Gateway / Secure Connection" screenshot aesthetic
- Client-side preferences: theme override, haptics on/off, reduced motion, infinite scroll toggle

### Backend
- FastAPI app (`backend/main.py`) with auto-mounted route folder
- Telegram `initData` HMAC-SHA256 verification (`app/auth.py`)
- Per-user daily rate limiter with admin overrides (`app/ratelimit.py`)
- MongoDB persistence with `miniapp_*` namespaced collections — cannot collide with the bot's collections
- Route modules:
  - `/api/profile/me` — identity + permissions + stats
  - `/api/search` — proxy to `hf_scraper.py` (or fallback direct nhentai)
  - `/api/gallery/{id}` — full detail
  - `/api/queue` (POST) + `/api/queue/status` (GET) — into the SAME queue admin_bot.py polls
  - `/api/bookmarks` — CRUD, per-user
  - `/api/admin/visibility` — public ↔ private toggle
  - `/api/admin/ratelimit/defaults` — global rate-limit config
  - `/api/admin/users` — list, reset, override, ban, unban
  - `/api/admin/diag` — scraper + queue probe
  - `/api/admin/stats` — KPI overview
  - `/api/random` — example route (Random tab)
- Service bridges: `scraper_bridge.py`, `queue_bridge.py` — isolate the Mini App from `hf_scraper.py` / `queue_service.py` API surface

### Deployment
- `start_patch.sh` — snippet to paste into your existing `start.sh` (replaces the dummy HTTP server)
- Optional standalone `Dockerfile` for split-service deployments
- `backend/.env.example` — documents every env var the Mini App recognises
- `backend/tests/smoke_test.py` — end-to-end smoke test against a live deployment

### Integration
- `integration/admin_bot_patch.py` — ready-to-paste `/app`, `/appon`, `/appoff` handlers for the existing bot

### Docs
- `README.md` — architecture overview + three-registries explanation
- `docs/INTEGRATION.md` — Render deployment + BotFather wiring recipe
- `docs/PLUGIN_GUIDE.md` — seven "how do I add/remove/modify X?" cheatsheets
- `docs/API.md` — full endpoint reference
- `CHECKPOINTS.md` — recovery ledger with every checkpoint URL
- `CHANGELOG.md` — this file
