# CHECKPOINTS — v12.4 (Turso cache-first + prefetch cron)

Session date: 2026-08-09
Baseline resumed from: v12_4_30_pct.zip (Turso client + cache module already shipped)
Finish line: FixPack_v12_4_final.zip + verify_v2.sh green (43/43)

Every line below is the file-wrapper URL published in chat at that
checkpoint. AI Drive mirror was NOT used this session — the URLs
alone are authoritative recovery artifacts (Rule 3, `URL is enough`).

| Pct | What landed | File-wrapper URL |
| ---:|:------------|:-----------------|
|  30 | Baseline (Turso client + nhentai_cache Turso-first, from prior session) | https://www.genspark.ai/api/files/s/xYkJRPgj |
|  35 | prefetch_cron.py scaffold — constants, _SORTS, env knobs, _enabled(), _bootstrap_paths(), _last_run dict + last_run_summary(); stub coroutines | https://www.genspark.ai/api/files/s/C5f0Rpo9 |
|  45 | _cache_key_for(sort,page) → `search:<sort>:page<N>` (aligns TTL_SEARCH_SEC 3d); async _fetch_one_page mirrors _direct_nhentai_search shape, non-raising, 429/HTTP/JSON/non-dict all downgraded to None | https://www.genspark.ai/api/files/s/56IMSI28 |
|  55 | prefetch_once() sweep (bucket-gated via try_consume("search"), 429→skipped/hard errors→failed, cache PUT best-effort) + run_forever() loop (asyncio.Event early-wake, re-reads _enabled(), swallows non-Cancelled exceptions) + trigger_now() re-entrant guard | https://www.genspark.ai/api/files/s/iZrzEUv5 |
|  65 | worker.py wired: guarded module import + `asyncio.create_task(run_forever(), name="prefetch_cron")` spawned before "worker started, entering main loop" anchor | https://www.genspark.ai/api/files/s/kVC05Xl4 |
|  72 | admin_bot.py /prefetch status: guarded imports of prefetch_cron + turso_client, renders enabled/interval/max_pages/sorts + last sweep counters + turso health; /prefetch now is stubbed | https://www.genspark.ai/api/files/s/v9qtr1BR |
|  78 | /prefetch now real body (calls trigger_now, replies with fresh status); /help admin section line added: `/prefetch [status\|now]` | https://www.genspark.ai/api/files/s/7MYDCTr9 |
|  85 | Full regression green: compileall exit 0, verify_v2.sh 43/43, miniapp/verify.sh OK, tests_v2_smoke.py 43 assertions | https://www.genspark.ai/api/files/s/OtrDKQh2 |
|  95 | Hygiene sweep — verified zero CHECKPOINTS*.md / Dockerfile / README_HF.md / DEPLOYMENT_GUIDE.md / __pycache__ / *.pyc / .git / .DS_Store in tree AND in the zip (belt-and-suspenders `-x` exclusions on zip) | https://www.genspark.ai/api/files/s/HE0zJEep |
| 100 | FixPack_v12_4_final.zip: prefetch_cron.py (543 lines), worker.py spawn wiring, admin_bot.py /prefetch handler + /help line, this CHECKPOINTS_v12_4.md ledger. Final regression re-run: 43/43 green. | (see final message) |

## Turso outage tolerance recap
* turso_client.turso_available() is checked on every call site;
  every function returns None/False on any exception.
* nhentai_cache.put() writes to BOTH backends best-effort; returns
  True if at least one succeeded.
* nhentai_cache.get() reads Turso first, Mongo second.
* prefetch_cron.prefetch_once() catches try_consume/put exceptions
  and fails-open — a broken cache never stops the sweep.
* worker.py wraps the prefetch spawn in try/except; a mini-app
  import failure only skips the sweep, never blocks worker boot.

## Dependencies unchanged
No requirements.txt edits this session. libsql-client was already
pinned by the 30% baseline. No NHENTAI_API_KEY used. One Turso
account. Search paginator ceiling preserved at page 20 (untouched).

## New env vars (all optional, safe defaults)
| Var | Default | Purpose |
|-----|--------:|---------|
| PREFETCH_ENABLED       | 1 (on) | Master switch |
| PREFETCH_INTERVAL_SEC  | 21600  | 6 h between sweeps |
| PREFETCH_MAX_PAGES     | 10     | Pages per sort per sweep |
| PREFETCH_DELAY_SEC     | 1.0    | Delay between fetches (polite) |

## New admin command
    /prefetch                 → alias for /prefetch status
    /prefetch status          → last sweep summary + turso health
    /prefetch now             → fire one sweep immediately
