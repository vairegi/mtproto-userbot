# v12.34d — Checkpoint Ledger (per RULES.txt §6)

| % | What was added | File-wrapper URL | AI Drive mirror |
|---|---|---|---|
| 50% | Patch applied: `_mongo_get` coerces every expires_at shape to epoch float (int/float/naive-datetime/aware-datetime); commit d64edee; py_compile OK; import smoke OK; purge script DRY_RUN smoke OK | (see final chat message) | pending (session-ephemeral) |
| 100% | Final bundle: patched nhentai_cache.py + new purge script + GUIDE.md v12.34d section + this ledger | (see final chat message) | pending (session-ephemeral) |

## Notes
- Push to GitHub main pending operator auth (sandbox has no GitHub credentials).
- Working repo: github.com/vairegi/mtproto-userbot @ HEAD c4c13e0 → d64edee+.
- Deploy = copy 2 files to their existing paths + commit + push. Only BOT 0 redeploys.
- Post-deploy: run `MONGO_URI=... python scripts/purge_mongo_nhentai_cache.py` ONCE.
