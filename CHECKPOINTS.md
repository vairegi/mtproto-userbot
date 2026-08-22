# v12.34c — Checkpoint Ledger (per RULES.txt §6)

| % | What was added | File-wrapper URL | AI Drive mirror |
|---|---|---|---|
| 50% | Patch applied: turso_client.read_your_writes=True + execute() None-log; nhentai_cache._turso_get diagnostic branches; scraper_bridge gate-miss warning; py_compile OK; import smoke OK | https://www.genspark.ai/api/files/s/PLACEHOLDER_50 | pending (session-ephemeral) |
| 100% | Final bundle: 3 patched files + GUIDE.md v12.34c section + this ledger | (see final chat message) | pending (session-ephemeral) |

## Notes
- Push to GitHub main pending operator auth (sandbox has no GitHub credentials).
- Working repo: github.com/vairegi/mtproto-userbot (verified against HEAD 64a1b5d).
- Deploy: copy 3 files over `miniapp/backend/app/services/`, commit, push. Only BOT 0 redeploys.
- Ground-truth Turso probe confirms rows physically exist with correct expires_at, so the fix is BOT 0-side read-path plumbing, NOT BOT 1 writes.
