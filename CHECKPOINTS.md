# Checkpoint ledger

Every intermediate recovery archive produced while migrating this project
from SQLite + PM2 to MongoDB Atlas + `start.sh`.

| %   | Milestone                                                             | File-wrapper URL                                    | AI Drive mirror                                          |
|-----|-----------------------------------------------------------------------|-----------------------------------------------------|----------------------------------------------------------|
| 20% | New MongoDB `db.py` (drop-in replacement) + `requirements.txt`        | https://www.genspark.ai/api/files/s/vZ5bnx3V        | /mtproto_userbot_checkpoints/MTProto_Recovery_20pct.zip  |
| 40% | `queue_service.py`, `config.py`, `startup_check.py`, `logging_setup.py` migrated + 128-test suite passing | https://www.genspark.ai/api/files/s/t3brvJqm | /mtproto_userbot_checkpoints/MTProto_Recovery_40pct.zip |
| 80% | `start.sh`, `userbot.py`, `Dockerfile`, `README_HF.md`, `.env.example`, `.gitignore` + all 14 modules import clean | https://www.genspark.ai/api/files/s/eiX5sUaW | /mtproto_userbot_checkpoints/MTProto_Recovery_80pct.zip |
| 100%| Final deliverable — `DEPLOYMENT_GUIDE.md` added + cleanup             | (see final message)                                 | (see final message)                                      |

## Verification performed at each stage

- Compile check (`python3 -m py_compile`) on every Python file.
- Bash syntax check (`bash -n start.sh`).
- Functional test of `db.py` against the real MongoDB API surface
  (`tests_db_mongo.py`, 128 assertions, all green).
- Whole-project import test (`test_imports.py`): every one of the 14 modules
  loads cleanly on top of the new MongoDB `db.py`.
- Dependency-resolution dry run: `pymongo[srv]==4.9.2 dnspython==2.7.0
  motor==3.6.0` verified as installable together (the naive `pymongo==4.10.1`
  pair produces a `ResolutionImpossible` error and would break the HF build).

## Recovery events

A sandbox recycle occurred between the 40% and 80% checkpoints. Recovery was
performed automatically by re-downloading `MTProto_Recovery_40pct.zip` from
the file-wrapper URL and continuing from the next milestone — no work was
lost.
