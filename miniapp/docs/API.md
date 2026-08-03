# API Reference

All endpoints require a valid Telegram `initData` payload in the `X-Telegram-Init-Data` header. The frontend attaches it automatically via `core/api.js`. Admin endpoints also require the caller's Telegram user id to match `ADMIN_USER_ID`.

Return shape is JSON. Errors return `{"detail": "message"}` with HTTP 4xx/5xx.

## Public endpoints

### `GET /api/profile/me`
Returns the caller's identity + permissions + rate-limit stats.

```json
{
  "user_id": 5428607127,
  "first_name": "Nameless",
  "username": "richmining",
  "photo_url": "https://…",
  "is_admin": false,
  "public_mode": true,
  "can_queue": true,
  "rate_limit": { "used": 3, "limit": 20, "unlimited": false, "cooldown_s": 0, "banned": false },
  "stats": { "bookmarks": 12, "queued": 3 },
  "banned": false
}
```

### `GET /api/search`
Query params:
- `q` — free text
- `include_tags` — comma-separated tag names
- `exclude_tags` — comma-separated tag names
- `artist` — artist name
- `pages_min`, `pages_max` — page-count range
- `sort` — `popular` | `popular-week` | `popular-today` | `date`
- `lang` — default `english`
- `page` — 1-indexed page number
- `per_page` — default 25

Returns `{ items: [{id, title, cover, pages, tags}], page, per_page }`.

### `GET /api/gallery/{id}`
Full detail for one gallery. Returns 404 if not found.

### `POST /api/queue`
Body `{ "url": "https://nhentai.net/g/12345/" }`.
Rate-limited per user. Returns 429 on quota exceeded / cooldown active. Returns 403 in private mode for non-admins.

### `GET /api/queue/status`
Returns `{ pending, processing, completed, failed, recent: [...] }`.

### `GET /api/bookmarks`
Returns `{ items: [...] }`.

### `POST /api/bookmarks`
Body `{ id, title, cover, pages, tags }`. Idempotent upsert.

### `DELETE /api/bookmarks/{gallery_id}`
Removes a bookmark.

## Admin-only endpoints

All require the caller to be `ADMIN_USER_ID`. Others get 403.

### `GET /api/admin/visibility`
Returns `{ public_mode: true|false }`.

### `POST /api/admin/visibility`
Body `{ public_mode: true|false }`. Flips the app between public and admin-only mode.

### `GET /api/admin/ratelimit/defaults`
Returns `{ daily, cooldown_s }`.

### `POST /api/admin/ratelimit/defaults`
Body `{ daily, cooldown_s }`. Sets global defaults for new users.

### `GET /api/admin/users`
Returns `{ items: [{user_id, first_name, username, photo_url, banned, limit, used_today, last_seen}] }`.

### `POST /api/admin/users/{uid}/reset`
Resets today's usage counter for a user.

### `POST /api/admin/users/{uid}/limit`
Body `{ daily: <int> }`. Overrides the user's daily quota. `0` means unlimited.

### `POST /api/admin/users/{uid}/ban`
Bans a user (all their `/api/queue` calls return 403).

### `POST /api/admin/users/{uid}/unban`
Reverses a ban.

### `GET /api/admin/diag`
Runs `scraper_bridge.route_status()` and `queue_bridge.status_summary()`. Frontend renders the result verbatim in the admin panel.

## Auth details

- Init data is signed with `HMAC_SHA256("WebAppData", BOT_TOKEN)`. Signatures older than 24 hours are rejected.
- Dev escape hatch: if `BOT_TOKEN` is empty AND `ADMIN_USER_ID` is set, the backend synthesises the admin user so the frontend is testable in a plain browser. **Never leave `BOT_TOKEN` empty in production.**

## Adding a new endpoint

Drop a `foo.py` in `backend/app/routes/`. Declare a `router = APIRouter(prefix="/api/foo")`. It auto-mounts on next boot. See `PLUGIN_GUIDE.md` §5 for a full example.
