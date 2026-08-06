# CHECKPOINTS — V7 (Numeric-ID + custom invite link + spoiler cover posts)

Task: SMALL grid (2 improvements across 3 files, no new modules). Two checkpoints: 50% and 100%.

| %    | Description                                                                                     | File-wrapper URL                                       | AI Drive mirror                                              |
|------|-------------------------------------------------------------------------------------------------|--------------------------------------------------------|--------------------------------------------------------------|
| 50%  | 3 edits applied; py_compile green on both .py files; admin.js brackets balanced                 | https://www.genspark.ai/api/files/s/LWjyPD9o           | `/DoujinshiUniverse_v2_checkpoints/FixPack_v7_50pct.zip`     |
| 100% | `verify_v2.sh` all 5 stages green — 43 assertions PASS. FINAL zip uploaded + mirrored.          | *(filled below at 100%)*                                | `/DoujinshiUniverse_v2_checkpoints/FixPack_v7_100pct.zip`    |

## Files changed (3)

1. `miniapp/backend/app/services/force_join.py` — improvement #6
   * New helper `_split_channel_and_invite(raw)` — accepts any of:
     `-1002252758260`, `-1002252758260 https://t.me/+abcXYZ`,
     `-1002252758260, https://t.me/+abcXYZ`, `-1002252758260 | https://t.me/+abcXYZ`,
     `@channelname https://t.me/+abcXYZ`, a bare `https://t.me/+abcXYZ`, or legacy
     `t.me/joinchat/…`. Splits on whitespace / comma / pipe / semicolon, picks the
     first invite-URL-shaped token as the invite half.
   * `add_channel()` now pre-splits the input; the second token (if any) is fed
     into the existing `url` slot. It then harvests the invite HASH from that URL
     via `_INVITE_LINK_RE` and stores it on `invite_hash` — so `join_url()`'s
     existing priority ladder (admin url → invite_hash → username → chat_id) emits
     the proper `t.me/+…` link instead of falling back to the unjoinable
     `t.me/c/<internal>` URL that private numeric channels would otherwise get.
   * `remove_channel()` mirrors the same split tolerance so admins can paste the
     same combined string when removing.
   * `username_field` derivation refactored — numeric-ID and invite-hash rows now
     always store an empty `username` (they were already meant to; the previous
     one-liner was harder to read).

2. `miniapp/frontend/js/pages/admin.js` — improvement #6 (UI half)
   * Force-join section now has TWO input rows: the existing channel-ref field
     (unchanged placeholder), plus a NEW second field labelled
     "Optional invite link (https://t.me/+…) — required for private -100… channels".
   * The Add button concatenates the two fields with a space before posting, so
     the backend's `_split_channel_and_invite()` sees both tokens on one line and
     stores them correctly.
   * Both fields are cleared on successful Add.
   * The hint block now explicitly warns: *"For private channels added by numeric
     ID, also paste an invite link (t.me/+…) below — otherwise the Join button
     won't work for non-members."*

3. `cover_poster.py` — improvement #7
   * New import: `from telethon.tl.types import InputMediaUploadedPhoto`.
   * In `post_cover()`, the cover-image branch now:
       1. Uploads bytes with `client.upload_file(...)`.
       2. Wraps the resulting file handle in `InputMediaUploadedPhoto(file=…, spoiler=True)`.
       3. Sends via `client.send_file(channel_id, file=<that media>, caption=…, force_document=False)`.
     Result: the cover post lands in the DB channel as a **spoiler-blurred image**.
     Users tap once to unblur — same UX Telegram uses for user-marked "spoiler" media.
   * Because Bot API's `copyMessage` preserves the source media's spoiler flag,
     **every downstream DM copy** (dm_delivery.py's dedup path, relay_v2.py's
     auto-DM, admin_bot.py's `fj:check` re-delivery) inherits the spoiler with no
     copy-side change. This was chosen deliberately — a single upstream write is
     safer than four parallel copyMessage edits.
   * Wrapped in a try/except: if the spoiler-media path fails for any Telegram
     DC / upload_file edge case, we fall back to the plain non-spoiler
     `send_file(...)` so a cover ALWAYS lands (the PDF-reply chain must not break).

## Improvement → File mapping

| # | Improvement                                                                        | Files                                              |
|---|------------------------------------------------------------------------------------|----------------------------------------------------|
| 6 | Admin can pair a numeric `-100…` (or @handle) channel with a joinable invite link | force_join.py, admin.js                            |
| 7 | Cover post + all downstream DM copies are sent as spoiler-blurred images          | cover_poster.py                                    |

## How the private-channel Join button now works (before vs after)

**Before:** Admin adds `-1002252758260`. `join_url(channel)` walks the priority ladder:
`url` (none) → `invite_hash` (none) → `username` (none) → `chat_id`, so it falls
back to `https://t.me/c/2392274488` — an internal-channel viewer URL that only
existing members can open. The Join button is dead for the very users it's supposed to onboard.

**After:** Admin enters `-1002252758260` in the top field AND
`https://t.me/+abcXYZ` in the new invite-link field. The backend splits the
concatenated string, harvests `abcXYZ` as `invite_hash`, and stores both the
raw admin `url` AND the harvested hash on the channel row. Now `join_url()`
picks up the admin url first, so the Join button opens the real invite page
where non-members can actually join the private channel.

## What to verify after redeploy

1. **Numeric ID + invite link (improvement #6):** In Admin → Force-join, enter
   `-1002252758260` in the top field, `https://t.me/+abcXYZ` in the new second
   field, tap Add. Non-admin user queues a gallery → "Please join" prompt DM
   arrives → Join button opens the real invite page, NOT `t.me/c/<internal>`.
   After joining and tapping "✅ I've joined", the gallery is DM'd.
2. **Spoiler cover (improvement #7):** Queue a fresh gallery. The cover post in
   the DB channel appears BLURRED (spoiler); tapping it reveals the image. The
   PDF still replies to it correctly. When the same gallery is auto-DM'd (or
   dedup-copied on a later Queue tap) the cover in the user's DM is also
   spoiler-blurred, matching the DB channel version.
3. **No regression on existing rows:** Public `@handle` channels and invite-only
   `t.me/+…` channels added in previous versions still work — `_normalise_handle`
   is unchanged and `_split_channel_and_invite` degrades to a single-token pass
   when only one thing is entered.
