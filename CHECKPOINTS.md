# CHECKPOINTS.md — FixPack v2 (real root causes, verified)

Task size: **SMALL** (5 file edits, no new modules) → **50% + 100%** grid.

## Files changed (per bug)

| File | Bug | Purpose of change |
|------|-----|-------------------|
| `hf_scraper.py`                                       | Caption meta rows | `_tag_names_from_detail()` now returns `List[Dict[str,str]]` with `{name,type}` preserved (previously threw away the type AND dropped `language`/`category` entirely). `GalleryMeta.tags` shape widened accordingly. Added `_flatten_tag_names()` helper for legacy callers. |
| `relay_v2.py`                                         | Caption meta rows | `mark_completed()` now persists tags with their original type instead of forcing everything to `type='tag'`. This means later re-reads (Mini App, /mpost) get the same grouped shape. |
| `cover_poster.py`                                     | Caption meta rows | `post_cover()` no longer strips types when building `CoverPost.tags`. Now returns typed dicts end-to-end. `CoverPost.tags` docstring + type hint updated. |
| `miniapp/backend/app/services/dm_delivery.py`         | DM forward failing | Hardened: `copyMessage` → `forwardMessage` fallback for anything Telegram refuses to copy; special-case "bot can't initiate conversation" → surfaces a clear `/start` prompt via `reason`; normalises `error_code` on every envelope. |
| `miniapp/backend/app/routes/queue.py`                 | DM forward failing | `already_completed` branch now returns the real `reason` in both `message` and `delivery_error` (was hard-coded "DM delivery failed"). |
| `miniapp/frontend/js/plugins/card-actions.js`         | DM forward failing | Regex-matches `initiate conversation` / `/start` in the error and toasts `"👋 Send /start to the bot in DM first, then try again."`. Fixed a missed `close()` in the success branch. |

## Root-cause summary

**Caption missing Groups / Parodies / Artists / Languages / Categories rows** — `hf_scraper._tag_names_from_detail()` returned a **flat list of name strings** (no `type`), and even worse dropped `language` + `category` tags entirely. Downstream, every tag was bucketed as `type='tag'`, so `_format_caption` had nothing to put in the grouped rows and skipped them. Fix: preserve `type` all the way through hf_scraper → cover_poster → relay_v2 → Mongo.

**Userbot not forwarding cover + PDF to the requester's DM** — the delivery path was correct, but three real-world Telegram failure modes were being swallowed silently:
1. User hadn't sent `/start` to the admin bot → `Forbidden: bot can't initiate conversation` → the frontend just toasted "unknown".
2. Certain media types can't be `copyMessage`'d — no fallback to `forwardMessage` was in place.
3. `delivery_error` was hard-coded to a generic string, hiding the real Bot API `description`.

## Acceptance

- **Caption smoke test** — `_format_caption` produces all six rows (Groups, Parodies, Artists, Languages, Categories, Tags) with typed input; caption length 252 chars; no nhentai URL anywhere.
- **DM delivery** — hardened path: `copyMessage` → `forwardMessage` fallback; clear reason on `Forbidden`; frontend guides the user to `/start` the bot.
- **`verify_v2.sh`** — all 5 stages green, 43 `tests_v2_smoke.py` assertions PASS.

| %    | Description | File-wrapper URL | AI Drive mirror |
|------|-------------|------------------|------------------|
| 50%  | All 6 edits applied; py_compile green on every edited `.py`; JS parses clean; caption smoke-test shows all 6 grouped meta rows. | *(see chat)* | `/DoujinshiUniverse_v2_checkpoints/FixPack_v2_50pct.zip` |
| 100% | `verify_v2.sh` green on all 5 stages (required files, py_compile, grep tripwires, miniapp/verify.sh, tests_v2_smoke.py: 43 PASS). FINAL zip uploaded + mirrored. | *(FINAL URL — see chat)* | `/DoujinshiUniverse_v2_checkpoints/FixPack_v2_FINAL.zip` |
