# Doujinshi Universe — Telegram Mini App

A Telegram Mini App (Web App) frontend + FastAPI backend that plugs into the existing `admin_bot.py` / `hf_scraper.py` project. Users browse nhentai galleries visually inside Telegram and queue them to the channel through the same MongoDB queue the bot already uses.

## Design Principles (READ THIS FIRST)

This project is **built to be modified continuously**. Every feature is a self-contained module with three properties:

1. **Registered via a manifest** — nothing is hard-wired in `index.html` or `main.py`.
2. **Isolated CSS namespace** — components can be restyled without touching others.
3. **Backend routes live in one file each** — add/remove a route file, nothing else changes.

### The Three Registries

| Registry | Location | Purpose |
|---|---|---|
| Page registry | `frontend/js/core/registry.js` | Which pages exist, which tab-bar icons show them, ordering |
| Component registry | `frontend/js/core/components.js` | Reusable UI atoms (card, chip, sheet, toast) — swap the implementation without touching callers |
| Route registry | `backend/app/routes/__init__.py` | Backend endpoints — drop a `foo.py` into `routes/`, it auto-mounts |

### To add a feature later

- **New tab?** Create `frontend/js/pages/<name>.js`, add one line to `registry.js`.
- **New API endpoint?** Create `backend/app/routes/<name>.py` with a `router = APIRouter()`. It auto-mounts.
- **New action button on gallery cards?** Add a line to `frontend/js/plugins/card-actions.js` — no card code changes.
- **Restyle everything?** Edit `frontend/css/theme.css` — every color/spacing/radius is a CSS variable.

## Architecture

```
frontend/                        # Static files, served by same FastAPI app
  index.html                     # Shell only — loads core.js which boots everything
  css/
    theme.css                    # Design tokens (colors, spacing, radii, shadows)
    base.css                     # Reset + typography
    components.css               # Component styles, one section per component
  js/
    core/
      app.js                     # Boot sequence, initData handoff
      registry.js                # Page + tab registration
      components.js              # Component factory registry
      api.js                     # fetch() wrapper with auth headers
      telegram.js                # Telegram WebApp SDK helpers (haptics, theme, back-btn)
      state.js                   # Tiny reactive store
    components/                  # Reusable UI atoms
      card.js
      chip.js
      sheet.js
      toast.js
      skeleton.js
    pages/                       # One file per tab/page
      search.js
      bookmarks.js
      queue.js
      profile.js
      admin.js                   # Only shown to admin_user_id
    plugins/                     # Optional feature slots
      card-actions.js            # Which buttons appear on each gallery card
      search-operators.js        # Which inline operators the search bar understands

backend/
  main.py                        # FastAPI app, mounts routes + serves frontend
  app/
    config.py                    # Env vars (reuses existing settings.*)
    auth.py                      # Telegram initData HMAC verify
    ratelimit.py                 # Per-user quota, admin-adjustable
    db.py                        # Reuses existing db.py from parent project
    routes/
      __init__.py                # Auto-discovers *.py in this folder
      search.py
      gallery.py
      queue.py
      bookmarks.py
      profile.py
      admin.py
    services/
      scraper_bridge.py          # Adapter around hf_scraper.py

docs/
  INTEGRATION.md                 # How to wire this into the existing bot
  PLUGIN_GUIDE.md                # How to add new features cleanly
  API.md                         # Endpoint reference
```

## Deployment

Same Render web service as the bot. The existing `start.sh` gets one new line: `uvicorn backend.main:app --port 8000 &`. The dummy HTTP server on `$PORT` is replaced by the actual Mini App backend, which also passes Render's port scan for free.

See `docs/INTEGRATION.md` for details.
