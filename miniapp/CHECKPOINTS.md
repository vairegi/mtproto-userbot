# Doujinshi Universe — Checkpoint Ledger

Every checkpoint below is a downloadable ZIP of the working state at that milestone. If the sandbox is ever recycled or a build breaks, download the latest ZIP and resume from there.

| % | What was added | File-wrapper URL | AI Drive mirror |
|---|---|---|---|
| 45% | Recovered 41 files from prior turn: full frontend shell (index.html, theme/base/components CSS, core JS registries, all components, all pages) + full backend (main.py, config, auth, db, ratelimit, all routes, service bridges) + INTEGRATION.md | https://www.genspark.ai/api/files/s/H5E9GhJ1 | /DoujinshiUniverse_checkpoints/DoujinshiUniverse_Recovery_45pct.zip |
| 55% | Added PLUGIN_GUIDE.md, API.md, CHECKPOINTS.md ledger, start.sh integration patch | https://www.genspark.ai/api/files/s/U1UfSBJi | /DoujinshiUniverse_checkpoints/DoujinshiUniverse_55pct.zip |
| 65% | Added Dockerfile, preview-modal plugin (first-page peek), Telegram back-button stack (sheets dismiss before app exits) | https://www.genspark.ai/api/files/s/v08aoG4X | /DoujinshiUniverse_checkpoints/DoujinshiUniverse_65pct.zip |
| 75% | Added `.env.example`, backend smoke_test.py, `/api/random` example route, `_healthz.py` skip-me example | https://www.genspark.ai/api/files/s/nXpcvENF | /DoujinshiUniverse_checkpoints/DoujinshiUniverse_75pct.zip |
| 85% | Added client-side prefs (haptics/theme/motion toggles), Settings tab, `/api/admin/stats` KPI endpoint + admin Overview section | https://www.genspark.ai/api/files/s/cZttFlvz | /DoujinshiUniverse_checkpoints/DoujinshiUniverse_85pct.zip |
| 95% | Added `integration/admin_bot_patch.py` (ready-to-paste `/app` `/appon` `/appoff` handlers) + CHANGELOG.md | https://www.genspark.ai/api/files/s/P5HpWbd6 | /DoujinshiUniverse_checkpoints/DoujinshiUniverse_95pct.zip |
| 100% | Final polish: ledger completed with all URLs, verification script | *(final upload — see chat)* | *(final upload — see chat)* |

## How to recover after a sandbox reset

```bash
# 1. Download the most recent checkpoint ZIP (top of the table)
curl -L "<file-wrapper URL>" -o recovery.zip

# 2. Extract next to the bot repo
unzip recovery.zip -d /path/to/mtproto-userbot/

# 3. Install extra Python deps
pip install -r mtproto-userbot/miniapp/backend/requirements.txt

# 4. Restart the Render service
```

## File count history

| Checkpoint | Files in zip |
|---|---:|
| 45% | 54 |
| 55% | 58 |
| 65% | 61 |
| 75% | 67 |
| 85% | 70 |
| 95% | 73 |
| 100% | 74 |
