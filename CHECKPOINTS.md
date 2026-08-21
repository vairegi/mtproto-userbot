# v12.34 — Checkpoint Ledger (per RULES.txt §6)

| % | What was added | File-wrapper URL | AI Drive mirror |
|---|---|---|---|
| 50% | Task 1 complete: db.get_cached_gallery_ids + _badge.py + 5 list routes wired + card.js pill + CSS | https://www.genspark.ai/api/files/s/K8Yak1vl | /mtproto-userbot_checkpoints/v12.34_Recovery_50pct.zip |
| 80% | Task 2 complete: cover_poster split (prepare_cover + post_prepared_cover), relay_v2 reordered to ONE channel-lock window, wait_task hack removed | https://www.genspark.ai/api/files/s/jajcyQsU | /mtproto-userbot_checkpoints/v12.34_Recovery_80pct.zip |
| 95% | config.VERSION = "v12.34", GUIDE.md v12.34 section; full verification pass (11 py files compile, pyflakes clean) | https://www.genspark.ai/api/files/s/JwBTed7r | /mtproto-userbot_checkpoints/v12.34_Recovery_95pct.zip |
| 100% | Final deployable bundle (this ZIP) + this ledger | (see final chat message) | /mtproto-userbot_checkpoints/v12.34_bundle.zip |

## Notes
- Early micro-checkpoints at 10%/25%/35% were re-executed after two sandbox
  recycles wiped un-checkpointed state (RULE 4 recovery). The 50% ZIP is the
  first durable artifact; everything before it is contained within it.
- Deploy: copy files over repo root, commit, push. No new env vars, no new
  processes, no new deps. Both Render services redeploy from the same commit.
