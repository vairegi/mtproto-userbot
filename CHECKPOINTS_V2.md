# V2 Rebuild — Checkpoint Ledger

Bot 1 removed, MongoDB `galleries` dedup gate installed, mini-app refit,
in-house cover poster, admin `/search` redirected to Mini App, verify
harness + smoke tests, .env docs.

All checkpoint zips authenticated via file-wrapper URLs and mirrored to
`/DoujinshiUniverse_v2_checkpoints/` on AI Drive.

| %   | Description                                                                                                   | file-wrapper URL                              | AI Drive mirror                                                   |
|-----|---------------------------------------------------------------------------------------------------------------|-----------------------------------------------|-------------------------------------------------------------------|
| 1%  | Recovery of V1 repo + `docs/ARCHITECTURE_V2.md` (Bot 1 removed, Mongo dedup gate)                              | https://www.genspark.ai/api/files/s/l5m92ug2  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_1pct.zip   |
| 2%  | `docs/MIGRATION_V2.md` (zero-downtime deploy sequence, rollback via SELF_COVER_POST_ENABLED)                   | https://www.genspark.ai/api/files/s/XMbyLEry  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_2pct.zip   |
| 5%  | `db.py` extended with `galleries` collection accessor + 3 indexes                                              | https://www.genspark.ai/api/files/s/YYzl3qT2  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_5pct.zip   |
| 10% | `gallery_state.py` — atomic dedup, six-status state machine, lazy 15-min stale reset, DedupDecision            | https://www.genspark.ai/api/files/s/QtAeqzEE  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_10pct.zip  |
| 15% | `cover_poster.py` — scrape+download+post cover via userbot, delete_cover, t.me/c deep-link builder             | https://www.genspark.ai/api/files/s/LGEGNk2i  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_15pct.zip  |
| 20% | `bot2_client.py` — thin @Gallery_DLBot helper (send_link, wait_for_pdf, dm_and_wait, Bot2Outcome)              | https://www.genspark.ai/api/files/s/Jz1gEpB7  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_20pct.zip  |
| 25% | `relay_v2.py` — full V2 orchestrator (dedup → cover → Bot 2 → forward → mark_completed) with rollback valve    | https://www.genspark.ai/api/files/s/4PpZLxVU  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_25pct.zip  |
| 30% | `worker.py` rewired: V1/V2 router via SELF_COVER_POST_ENABLED, `submitted_by` forwarded                        | https://www.genspark.ai/api/files/s/nyOTFDKV  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_30pct.zip  |
| 35% | Dedup-hit delivery parity: cached open_link stamped on new job row via set_cover_link, mark_status("done")     | https://www.genspark.ai/api/files/s/Rv20hcYf  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_35pct.zip  |
| 40% | Failure-path hardening: user-vs-admin message split (3 `USER_MSG_*` constants), admin DMs on scrape/timeout    | https://www.genspark.ai/api/files/s/hQaKJFIq  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_40pct.zip  |
| 45% | Mini-app enqueue dedup gate: `dedup_peek` runs BEFORE rate-limit consume, returns already_completed/processing | https://www.genspark.ai/api/files/s/4MXer22M  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_45pct.zip  |
| 50% | Read-only `/api/gallery/{id}/status` endpoint + `v2_status` enrichment on GET `/api/gallery/{id}`              | https://www.genspark.ai/api/files/s/x4qjSdy3  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_50pct.zip  |
| 55% | card-actions.js "Queue" ⇄ "Open Post" swap; search.js + bookmarks.js unwrap function-valued action fields      | https://www.genspark.ai/api/files/s/QbGzRsL7  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_55pct.zip  |
| 60% | Mini-app Queue tab surfaces COMPLETED deep-links; queue_bridge._row enriched with open_link/gallery_id         | https://www.genspark.ai/api/files/s/t8g3dZIu  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_60pct.zip  |
| 65% | admin_bot.py `/search` redirects to Mini App with inline WebAppInfo button + query pre-fill                    | https://www.genspark.ai/api/files/s/tU8ba1fB  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_65pct.zip  |
| 70% | `BOT1_USERNAME` soft-deprecated in config.py + `_emit_bot1_deprecation_warning()` at import                    | https://www.genspark.ai/api/files/s/4IMlPknz  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_70pct.zip  |
| 75% | `scripts/migrate_v2_recover_stuck.py` (idempotent orphan sweeper) + `STATUS_FAILED_RECOVERED` retryable        | https://www.genspark.ai/api/files/s/sH9XKkld  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_75pct.zip  |
| 80% | `miniapp/docs/INTEGRATION.md` extended with V2 addendum (envs, endpoints, client behaviour, verification)      | https://www.genspark.ai/api/files/s/4tMgZG5d  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_80pct.zip  |
| 85% | `tests_v2_smoke.py` (43 assertions, 0 network) covering dedup gate, mark_*, bot2_client, cover_poster          | https://www.genspark.ai/api/files/s/jNZWpAKa  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_85pct.zip  |
| 90% | `verify_v2.sh` 5-stage pre-deploy check (files, py_compile, tripwires, miniapp/verify.sh, smoke tests)         | https://www.genspark.ai/api/files/s/ss7Pblum  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_90pct.zip  |
| 95% | `.env.example` at repo root + `docs/ENV_REFERENCE.md` (V2 envs, deprecations, Render recommendations)          | https://www.genspark.ai/api/files/s/72JefLEQ  | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_Recovery_95pct.zip  |
| 100%| Final ledger (this file) + FINAL zip                                                                           | see final chat message                        | /DoujinshiUniverse_v2_checkpoints/DoujinshiV2_FINAL_100pct.zip    |

## Deliverable inventory

**New files** (12)
```
docs/ARCHITECTURE_V2.md
docs/MIGRATION_V2.md
docs/ENV_REFERENCE.md
gallery_state.py                       (state machine)
cover_poster.py                        (in-house cover post; replaces Bot 1)
bot2_client.py                         (thin @Gallery_DLBot helper)
relay_v2.py                            (V2 orchestrator)
scripts/migrate_v2_recover_stuck.py    (one-shot orphan sweeper)
tests_v2_smoke.py                      (43 assertions, offline)
verify_v2.sh                           (5-stage pre-deploy check)
.env.example                           (V2-annotated env template)
CHECKPOINTS_V2.md                      (this file)
```

**Modified files** (12)
```
db.py                                                (galleries accessor + 3 indexes)
worker.py                                            (V1/V2 router; submitted_by threaded)
config.py                                            (BOT1_USERNAME soft-deprecation)
admin_bot.py                                         (/search -> Mini App button)
miniapp/backend/app/routes/queue.py                  (dedup gate before rate-limit)
miniapp/backend/app/routes/gallery.py                (/status endpoint + v2_status enrichment)
miniapp/backend/app/services/queue_bridge.py         (dedup_peek + gallery_status + _row open_link)
miniapp/frontend/js/plugins/card-actions.js          (dynamic Queue⇄Open Post label)
miniapp/frontend/js/pages/search.js                  (function-value unwrap + status pre-fetch)
miniapp/frontend/js/pages/bookmarks.js               (same unwrap + status pre-fetch)
miniapp/frontend/js/pages/queue.js                   (Open Post buttons for done rows)
miniapp/docs/INTEGRATION.md                          (V2 addendum sections V2-a..V2-f)
```

**Untouched (V1 code path preserved for rollback)**
```
relay.py, hf_scraper.py, queue_service.py, search_picker.py,
progress_tracker.py, source_api.py, userbot.py, url_utils.py,
logging_setup.py, startup_check.py, tests_db_mongo.py, start.sh, Dockerfile
```

## Verification signal at 100%

```
verify_v2.sh — Stage results:
  1. V2 required files (24)             PASS
  2. Root Python py_compile (21)        PASS
  3. Regression tripwires (12)          PASS
  4. miniapp/verify.sh (delegated)      PASS
  5. tests_v2_smoke.py (43 assertions)  PASS

Overall: ✓ V2 verification passed. Safe to deploy.
```

## Deploy in three commands

```bash
# 1. From a fresh checkout of the FINAL zip:
bash verify_v2.sh                                   # confirm green

# 2. Preview the one-time migration (safe, no writes):
python3 scripts/migrate_v2_recover_stuck.py --dry-run

# 3. Push + let Render auto-deploy; then run the migration for real:
python3 scripts/migrate_v2_recover_stuck.py
```

Rollback: set `SELF_COVER_POST_ENABLED=0` in Render, redeploy. The V1
(Bot 1) code path is preserved untouched — worker.py's router picks it
up on the next tick.
