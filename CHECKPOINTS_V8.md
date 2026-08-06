# CHECKPOINTS — V8 (Force-join re-add + Remove button + collapsible Users)

Task: SMALL grid (3 fixes across 2 files, no new modules). Two checkpoints: 50% and 100%.

| %    | Description                                                                                     | File-wrapper URL                                       | AI Drive mirror                                              |
|------|-------------------------------------------------------------------------------------------------|--------------------------------------------------------|--------------------------------------------------------------|
| 50%  | 3 edits applied; py_compile green on force_join.py; admin.js brackets balanced                  | https://www.genspark.ai/api/files/s/fFj7nNxl           | `/DoujinshiUniverse_v2_checkpoints/FixPack_v8_50pct.zip`     |
| 100% | `verify_v2.sh` all 5 stages green — 43 assertions PASS. FINAL zip uploaded + mirrored.          | *(filled below at 100%)*                                | `/DoujinshiUniverse_v2_checkpoints/FixPack_v8_100pct.zip`    |

## Root-cause diagnosis of each reported bug

### Bug 1 — Join button still emits `https://t.me/c/2252758260`

Root cause was NOT the split-input parser. The v7 parser works correctly on a fresh Add. The failure was on an **existing row that was added BEFORE the invite-link field was introduced**:

* Old row stored: `{chat_id: -1002252758260, invite_hash: null, url: null, username: ""}`.
* Admin re-Adds the SAME channel with an invite link filled in this time.
* v7's `add_channel()` matched the existing row and returned early with
  `{"already": True, "channels": current}` — no update was performed.
* `join_url(channel)` walks admin_url → invite_hash → username → chat_id, all empty
  except `chat_id`, and falls back to `https://t.me/c/2252758260` (unjoinable
  internal-channel viewer URL that only existing members can open).

Verdict: the row on disk never gained the invite hash / URL, so the Join button
was stuck emitting the bad link.

### Bug 2 — Remove button silently no-ops on invite-hash rows

* `admin.js` derived the row key as `c.username || String(c.chat_id || "")`.
* Invite-hash-only rows (e.g. "Private channel (+uyNxVA…)") have `username=""`
  AND `chat_id=null`, so `key` was `""`.
* The click handler had `if (!key) return;` → the button silently did nothing.

### Bug 3 — Users section takes too much space

Not a bug per se, but the (potentially long) user list is always expanded on
open, and the `/api/admin/users` request fires on every Admin-tab render.

## Files changed (2)

1. `miniapp/backend/app/services/force_join.py` — Bug 1 fix
   * `add_channel()` no longer returns early with `already=True` when a
     matching row exists. Instead it now merges any NEW information into
     the existing row and re-persists:
       * `url` — set/overwritten when the admin provided one.
       * `invite_hash` — set when we can harvest a fresh hash from the URL.
       * `chat_id` — filled in when it was previously null.
       * `title` — updated when the admin gave a fresh explicit title.
   * Response now carries `updated: True` so the frontend can toast
     differently ("Channel updated" vs "Already in the list"), though the
     current UI just says "Channel added" for either path.
   * The match logic was refactored into a small inner `_matches(c)` helper
     for readability — same three matching rules as v7 (`invite_hash`,
     `chat_id`, `username`).

2. `miniapp/frontend/js/pages/admin.js` — Bugs 2 & 3
   * **Bug 2 (Remove button):**
     * Row label now falls back to `"Private channel (+<hash6>…)"` when
       there is no `title`/`username`/`chat_id` (so invite-hash-only rows
       have a real label even before Bot API resolves them).
     * Remove-button key derivation now falls back all the way to
       `"https://t.me/+<invite_hash>"` — the backend's
       `_split_channel_and_invite()` + `_normalise_handle()` already decode
       that shape correctly. The empty-key guard now toasts an explicit
       "Cannot identify this row — please reload" instead of silently
       returning.
     * Toast text now reflects the `r.removed` flag from the backend
       ("Channel removed" only when something actually left the list).
   * **Bug 3 (Collapsible Users):**
     * `sectionUsers()` now renders a clickable `<h3>` header with a
       rotating ▼ caret (points right when collapsed, down when expanded).
     * Body is `display: none` by default (COLLAPSED on open).
     * The `/api/admin/users` fetch is now **lazy** — it only runs on the
       first expand, not on Admin-tab render. Every subsequent expand
       just shows the cached list; the Reset/Set/Ban buttons still trigger
       a refresh as before.
     * Header is keyboard-accessible (`role="button"`, `tabindex="0"`,
       `aria-expanded`, Enter/Space toggle) and gives a light haptic pulse
       on toggle.

## Improvement → Fix mapping

| Bug | Symptom                                                              | Files                     |
|-----|----------------------------------------------------------------------|---------------------------|
| 1   | Join button emits `t.me/c/<internal>` even after re-Add with link    | force_join.py             |
| 2   | Remove button does nothing on invite-hash rows                       | admin.js                  |
| 3   | Users list can't be hidden; loads on every Admin-tab render          | admin.js                  |

## What to verify after redeploy

1. **Bug 1 fix — Re-Add updates the row:**
   In Admin → Force-join, find your existing "-1002252758260" row. In the
   top field enter `-1002252758260`, in the invite-link field paste your
   real `https://t.me/+abcXYZ` invite link, tap Add. Non-admin user
   queues a gallery → "Please join" prompt DM arrives → the Join button
   opens the REAL invite page (t.me/+…), NOT `t.me/c/2252758260`.
2. **Bug 2 fix — Remove works on private rows:**
   Tap Remove on the "Private channel (+uyNxVA…)" row. Toast reads
   "Channel removed" and the row disappears from the list. Repeat for
   your DAILY DOUJINSHI DOSE row.
3. **Bug 3 fix — Users section collapses:**
   Open the Admin tab: the 👥 Users heading is visible but the user list
   is HIDDEN by default (▼ caret points right). Tap the heading: caret
   rotates down and the list loads (Loading users… → rows). Tap again:
   the list is hidden and the caret goes back. Cached rows reappear
   instantly on subsequent expands.
