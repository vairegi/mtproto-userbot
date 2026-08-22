# v12.34b — Checkpoint Ledger (per RULES.txt §6)

| % | What was added | File-wrapper URL | AI Drive mirror |
|---|---|---|---|
| 50% | Patch applied: bot0_hints.py (new, 132 lines) + mongo_client.hint_pop_gids + details_sweeper step-0 + scraper_bridge._hint_push at 2 writesites; py_compile OK; import smoke OK; commit 5bf6553 | https://www.genspark.ai/api/files/s/PLACEHOLDER_50 | /mtproto-userbot_checkpoints/v12.34b_Recovery_50pct.zip |
| 100% | Final bundle: 4 patched/added files + GUIDE.md v12.34b section + this ledger | (see final chat message) | /mtproto-userbot_checkpoints/v12.34b_bundle.zip |

## Notes
- Push to GitHub main pending operator auth (sandbox has no GitHub credentials); fix committed locally as `5bf6553`, GUIDE.md entry as next short SHA, ledger as next short SHA.
- Working repo: github.com/vairegi/mtproto-userbot (commit 1702edd → 5bf6553+). telegram-file-bot ignored per operator instruction.
- Deploy: copy 4 files into repo root (bot0_hints.py at root, the others at their existing paths), commit, push. No new env vars. Both Render services redeploy from the same commit.
