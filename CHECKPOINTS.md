# CHECKPOINTS.md — FixPack v3 (auto-DM + rich detail sheet)

Task size: **SMALL** (2 file edits) → **50% + 100%** grid.

## Files changed (per bug)

| File | Bug | Purpose of change |
|------|-----|-------------------|
| `relay_v2.py` | Bot doesn't auto-DM the requester after fresh post | Added `_auto_dm_requester()` helper and a step 9 in `process_job()` that fires it right after `mark_completed`. Uses the userbot session's `forward_messages(from_peer=channel, drop_author=True)` — works even if the requester never `/start`'d the admin bot. Skips when requester == admin. Best-effort: any failure is logged, job still returns DONE. |
| `miniapp/backend/app/services/scraper_bridge.py` | Detail sheet only shows title | Rewrote `gallery_detail()` to ALWAYS prefer the direct nhentai v2 detail endpoint (only source for `title_english`, `title_japanese`, `favorites`, `upload_date`, and grouped tag rows). Added a `groups: {type → [{name}]}` field so `detail-sheet.js` renders labelled meta rows immediately. Fallback to hf_scraper still synthesises `groups` from typed tags so the sheet is never empty. |

## Root-cause summary

**Auto-DM missing:** `relay_v2.process_job()` posted the cover + PDF into the DB channel and stopped there — nothing forwarded the pair into the requester's DM until they tapped Queue a SECOND time (which then hit the dedup branch's `dm_delivery.deliver_to_dm`). The requester had `submitted_by` in scope the whole time; we just weren't using it. Fix uses the userbot (not the admin bot) so it works regardless of `/start` state.

**Detail sheet blank below title:** the frontend `detail-sheet.js` was already coded to render `d.groups`, `d.title_english`, `d.title_japanese`, `d.favorites`, `d.upload_date` — but when `hf_scraper` was importable (normal case), `gallery_detail()` returned `_meta_to_dict(meta)` which only carries `{id, title, cover, pages, tags}`. All the rich fields were only available in the fallback `_direct_nhentai_detail()`. Fix: prefer the direct v2 endpoint for detail lookups (search still uses hf_scraper), and expose `groups` in both paths.

## Acceptance

- `python3 -m py_compile` — green on both edited files.
- `verify_v2.sh` — all 5 stages green, 43 `tests_v2_smoke.py` assertions PASS.

| %    | Description | File-wrapper URL | AI Drive mirror |
|------|-------------|------------------|------------------|
| 50%  | Both edits applied; py_compile green; `_meta_to_dict` typed-tag smoke test passes. | *(see chat)* | `/DoujinshiUniverse_v2_checkpoints/FixPack_v3_50pct.zip` |
| 100% | `verify_v2.sh` green on all 5 stages; 43 smoke assertions PASS; FINAL zip uploaded + mirrored. | *(FINAL URL — see chat)* | `/DoujinshiUniverse_v2_checkpoints/FixPack_v3_FINAL.zip` |
