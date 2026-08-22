# v12.39.5 — Checkpoint Ledger (per RULES.txt §6)

| % | What was added | File-wrapper URL | AI Drive mirror |
|---|---|---|---|
| 50% | Fix applied: nhentai_cache.py:38 `from . import db as _midb` → `from .. import db as _midb`; py_compile OK; import bm_cover_get/put OK; commit 19e2546 | https://www.genspark.ai/api/files/s/rFpV2KHz | /mtproto-userbot_checkpoints/v12.39.5_Recovery_50pct.zip |
| 100% | Final deployable bundle: fixed nhentai_cache.py + GUIDE.md v12.39.5 section + this ledger | (see final chat message) | /mtproto-userbot_checkpoints/v12.39.5_bundle.zip |

## Notes
- Push to GitHub main pending operator auth (sandbox has no GitHub credentials); fix committed locally as `19e2546`, GUIDE.md entry as `fbea153`.
- Working repo: github.com/vairegi/mtproto-userbot (telegram-file-bot now holds a different tree — matches HANDOVER §10 warning).
- Deploy: copy nhentai_cache.py over repo root, commit, push. No new env vars, no new processes. Both Render services redeploy from the same commit.
