# CHECKPOINTS.md — FixPack v5 (3 new admin features)

Task size: **LARGE** (4 new modules + 6 edited files) → **50% + 100%** grid with the feature set treated as one milestone.

## Files created

| File | Purpose |
|------|---------|
| `feature_flags.py` (root) | Bot-side readers for the shared `miniapp_settings` store + Bot API helpers. Used by `relay_v2` (auto-DM) and `admin_bot` (force-join callback). Owns: auto_delete_enabled/hours, share_disabled, force_join_channels, schedule_deletes, check_membership, send_join_prompt, remember/pop_pending. |
| `miniapp/backend/app/services/deletion_scheduler.py` | Feature 1 — background loop (every 60s) that consumes `miniapp_scheduled_deletes` and Bot API `deleteMessage`s due rows. Started at FastAPI `startup`. |
| `miniapp/backend/app/services/force_join.py` | Feature 3 (mini-app side) — settings I/O, getChat/getChatMember gate, join-button keyboard builder, pending-delivery memory in `miniapp_pending_deliveries`. |
| `miniapp/backend/app/services/share_guard.py` | Feature 2 — reads the `share_disabled` toggle and produces `{"protect_content": True}` to merge into every send/copy payload. |

## Files edited

| File | Change |
|------|--------|
| `miniapp/backend/app/services/dm_delivery.py` | Force-join gate before every delivery (sends the Join prompt + remembers pending); `protect_content` via share_guard on every copy/forward; captures new `message_id`s from Bot API responses and schedules them for auto-delete on success. |
| `miniapp/backend/app/routes/admin.py` | New endpoints: `GET/POST /api/admin/autodelete`, `GET/POST /api/admin/shareguard`, `GET /api/admin/forcejoin`, `POST /api/admin/forcejoin/add`, `POST /api/admin/forcejoin/remove`. |
| `miniapp/backend/app/routes/queue.py` | Surfaces `blocked_by_force_join` + the join prompt message on both `POST /api/queue` (dedup branch) and `POST /api/queue/deliver/{id}` instead of treating it as a delivery failure. |
| `miniapp/backend/main.py` | FastAPI `startup` hook starts the deletion_scheduler background loop (idempotent). |
| `miniapp/frontend/js/pages/admin.js` | Three new sections: ⏱️ Auto-delete (toggle + hours + Save), 🔒 Disable sharing (toggle), 👥 Force-join (channel list + Add + Remove). |
| `miniapp/frontend/js/plugins/card-actions.js` | Handles `blocked_by_force_join` responses with a "Join the required channel(s) — check your DM" toast instead of an error toast. |
| `relay_v2.py` | Auto-DM path now: force-join gate first (prompt + remember pending), share-disabled → protect_content on copyMessage, collects sent message_ids, schedules auto-delete rows, pops force-join pending on success. `import feature_flags` + `gallery_id` param on `_auto_dm_requester`. |
| `admin_bot.py` | New `cb_force_join` callback (pattern `^fj:`) — re-checks membership on "✅ I've joined", delivers ALL remembered pending galleries via copyMessage (with protect_content), sends "📨 Sent to your DM", schedules auto-delete, pops pending rows. `import feature_flags`, `gallery_state as _gs`. Handler registered before the catch-all swallow. |

## How each feature works end-to-end

**1. Auto-delete** — Admin sets toggle + hours in Mini App Admin tab. Every successful DM delivery (auto-DM after fresh queue, /api/queue/deliver, force-join recheck) records the new message_ids into `miniapp_scheduled_deletes` with `delete_at = now + hours`. The mini-app backend's background loop deletes them via `deleteMessage` every 60s.

**2. Disable sharing** — Admin toggles it on. From that moment, every copyMessage/forwardMessage/sendMessage the bot sends includes `protect_content: true` — Telegram blocks the recipient from forwarding or saving those messages.

**3. Force-join** — Admin adds channels (@handle or -100… ID) in the Admin tab. Before ANY DM delivery, the backend calls `getChatMember` for each. If the user is missing any, delivery is refused; instead the bot DMs them a prompt with 🔗 Join buttons + an "✅ I've joined" button. Tapping it re-checks membership; on success the remembered gallery is delivered automatically. Channels that can't be verified (bot not admin there) fail OPEN so a misconfig never locks everyone out.

## Acceptance

- `python3 -m py_compile` — green on all 10 edited/new Python files.
- JS parse (`new Function`) — green on `admin.js` + `card-actions.js`.
- `verify_v2.sh` — all 5 stages green, 43 `tests_v2_smoke.py` assertions PASS.

| %    | Description | File-wrapper URL | AI Drive mirror |
|------|-------------|------------------|------------------|
| 50%  | All 4 new modules + 6 edited files applied; py_compile + JS parse green. | *(see chat)* | `/DoujinshiUniverse_v2_checkpoints/FixPack_v5_50pct.zip` |
| 100% | `verify_v2.sh` green on all 5 stages; 43 smoke assertions PASS; FINAL zip uploaded + mirrored. | *(FINAL URL — see chat)* | `/DoujinshiUniverse_v2_checkpoints/FixPack_v5_FINAL.zip` |
