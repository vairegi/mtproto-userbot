# CHECKPOINTS — V6 (Force-join invite links + request-to-join + real titles + dedup DM confirmation)

Task: SMALL grid (5 edits across 5 files, no new modules). Two checkpoints: 50% and 100%.

| %    | Description                                                                                      | File-wrapper URL                                       | AI Drive mirror                                              |
|------|--------------------------------------------------------------------------------------------------|--------------------------------------------------------|--------------------------------------------------------------|
| 50%  | All 5 edits applied; `python3 -m py_compile` green on every edited .py; admin.js brackets balanced | https://www.genspark.ai/api/files/s/uHzjsGx0           | `/DoujinshiUniverse_v2_checkpoints/FixPack_50pct.zip`        |
| 100% | `verify_v2.sh` all 5 stages green — 43 assertions PASS. FINAL zip uploaded + mirrored.           | *(filled below at 100%)*                                | `/DoujinshiUniverse_v2_checkpoints/FixPack_100pct.zip`       |

## Files changed (5)

1. `miniapp/backend/app/services/force_join.py` — improvements #1, #2, #3
   * Added `import re` + `_INVITE_LINK_RE` regex (supports `t.me/+…` and legacy `t.me/joinchat/…`).
   * Rewrote `_normalise_handle()` to recognise invite links → `"invite:<hash>"`, strip `http(s)://` + `t.me/` + `@`.
   * `add_channel()` now accepts invite links, stores `invite_hash`, auto-defaults `url` to `https://t.me/+<hash>`, and best-effort prefills the real `title` via `_fetch_channel_title()`.
   * `remove_channel()` matches by `invite_hash` too.
   * `join_url()` priority: admin `url` → `invite_hash` → `username` → `chat_id`.
   * New `_fetch_channel_title()` (sync Bot API `getChat` → `result.title`).
   * `_resolve_chat_id()` also caches the fresh `title` back into settings.
   * New `_has_pending_join_request()` reads `miniapp_pending_join_requests` (populated by admin_bot.py).
   * `_is_member()` falls back to `_has_pending_join_request()` on either "not found" or a non-member status → request-to-join channels pass the gate.
   * `build_join_keyboard()` prefers `c["title"]`; if missing, calls `_fetch_channel_title(c)` once before falling back to the raw handle.
   * `send_join_prompt()` text updated to mention "or after requesting to join".

2. `feature_flags.py` — same four semantic changes as FILE 1, but async and against the bot-side `conn.db["miniapp_pending_join_requests"]`.
   * New `_has_pending_join_request(conn, user_id, chat_id)` (sync read of shared collection).
   * Rewrote `_is_member(conn, user_id, chat_id)` (new `conn` parameter) with the same pending-request fallback.
   * `check_membership(conn, user_id)` now passes `conn` down; invite-only channels that can't be resolved fail OPEN (skipped, not treated as missing).
   * `join_url()` honours `invite_hash`.
   * New async `_fetch_title(channel)` helper (Bot API `getChat`).
   * `send_join_prompt()` prefers `channel["title"]`, then fetches it, then falls back to handle; prompt text mentions "or after requesting to join".

3. `miniapp/backend/app/services/dm_delivery.py` — improvement #4
   * At the very end of `deliver_to_dm(...)`, when `result["delivered"]` is True, sends `sendMessage` via Bot API with text `📨 Sent to your DM` to the same `uid`, merging `**share_guard.payload()` so protect_content sticks when Disable-sharing is on.
   * Captures the returned `message_id` and appends it to `sent_msg_ids` so auto-delete schedules the confirmation too.
   * Wrapped in try/except that only logs on failure — never blocks the response.

4. `admin_bot.py` — server-side half of improvement #2
   * Added `ChatJoinRequestHandler`, `ChatMemberHandler` imports.
   * New `cb_chat_join_request()`: on request-to-join tap, upserts a row into `miniapp_pending_join_requests` with `status="pending"`.
   * New `cb_chat_member_update()`: on `ChatMemberHandler.ANY_CHAT_MEMBER` update, flips the row's `status` to `"approved"` on member statuses (creator/administrator/member/restricted), or deletes it on `"left"/"kicked"/"banned"`.
   * Both registered BEFORE the catch-all `MessageHandler(filters.ALL, swallow)`.
   * `allowed_updates=Update.ALL_TYPES` was already in place, so `chat_join_request` and `chat_member` updates are received.

5. `miniapp/frontend/js/pages/admin.js` — improvement #5
   * Force-join input placeholder now reads: `@channel  OR  https://t.me/+abcXYZ  OR  -1001234567890`.
   * Hint block updated to mention public @handle, private invite link (t.me/+…), or numeric -100… channel ID, and reminds admin the bot must be admin in each channel.

## Improvement → File mapping

| # | Improvement                                                              | Files                              |
|---|--------------------------------------------------------------------------|------------------------------------|
| 1 | Force-join accepts invite links (t.me/+… , t.me/joinchat/…)              | force_join.py, feature_flags.py    |
| 2 | Users with a pending request-to-join count as members                    | force_join.py, feature_flags.py, admin_bot.py |
| 3 | Join button label shows the real channel TITLE                           | force_join.py, feature_flags.py    |
| 4 | Dedup path DMs "📨 Sent to your DM" (not just in-app toast)              | dm_delivery.py                     |
| 5 | Admin tab placeholder + hint documents both @handle and t.me/+… formats  | admin.js                           |

## What to verify after redeploy

1. **Invite link + real title (improvements #1, #3):** Add a private channel via a `t.me/+…` invite link in the Admin tab. The button in the DM prompt should show the real channel title (e.g. "Doujinshi Community"), not the raw handle. Tap it → correct invite page opens.
2. **Request-to-join pending (improvement #2):** Enable "Request to join" on that channel, add it as a force-join channel, queue as a non-admin user, tap the invite link but do NOT approve the request. Queue again → user is NOT blocked (the pending request counts as membership).
3. **Dedup-DM confirmation (improvement #4):** Queue a gallery that is already in the DB channel. The `📨 Sent to your DM` confirmation must ACTUALLY arrive in Telegram DM (from the admin bot), not just as an in-app toast in the Mini App.
