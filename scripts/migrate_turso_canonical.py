"""
migrate_turso_canonical.py — v12.47 one-time canonical rewrite of legacy
nhentai_cache rows (gallery:* and search:*).

Usage (from repo root):
    python3 scripts/migrate_turso_canonical.py            # DRY RUN (report only)
    python3 scripts/migrate_turso_canonical.py --apply    # write canonical rows

Env: TURSO_DATABASE_URL, TURSO_AUTH_TOKEN. Optional: MIGRATE_MAX_PAGES
(default 0 = all; set e.g. 5 for a bounded dry run).

Safety: dry-run default; payload-only UPDATE (preserves cached_at /
expires_at / ttl_sec); rows failing validation are SKIPPED + listed,
never half-written; canonical rows untouched.
"""
from __future__ import annotations

import argparse, json, os, sys, time
import httpx

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from common.turso_cache.normalize import normalize_for_write  # noqa: E402

PAGE = 500


def _arg(v):
    if v is None: return {"type": "null"}
    if isinstance(v, bool): return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int): return {"type": "integer", "value": str(v)}
    if isinstance(v, float): return {"type": "float", "value": v}
    return {"type": "text", "value": str(v)}


def _cell(c):
    if not isinstance(c, dict): return c
    t = c.get("type")
    if t == "null": return None
    if t == "integer":
        try: return int(c.get("value") or 0)
        except (TypeError, ValueError): return 0
    return c.get("value")


def pipe(base, token, sql, args=None):
    body = {"requests": [
        {"type": "execute",
         "stmt": {"sql": sql, "args": [_arg(a) for a in (args or [])]}},
        {"type": "close"}]}
    r = httpx.post(base + "/v2/pipeline", json=body,
                   headers={"Authorization": f"Bearer {token}"}, timeout=45.0)
    r.raise_for_status()
    res = (r.json().get("results") or [{}])[0]
    if res.get("type") != "ok":
        raise RuntimeError(f"stmt error: {res.get('error')}")
    out = res["response"]["result"]
    cols = [c["name"] for c in out.get("cols", [])]
    return [{cols[i]: _cell(cell) for i, cell in enumerate(row)}
            for row in out.get("rows", [])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    max_pages = int(os.environ.get("MIGRATE_MAX_PAGES", "0"))

    base = (os.environ.get("TURSO_DATABASE_URL") or "").strip()
    for s in ("libsql://", "turso://", "wss://", "ws://"):
        if base.startswith(s):
            base = "https://" + base[len(s):]
    if "://" not in base:
        base = "https://" + base
    base = base.rstrip("/")
    token = (os.environ.get("TURSO_AUTH_TOKEN") or "").strip()
    if not base or not token:
        print("TURSO_DATABASE_URL / TURSO_AUTH_TOKEN required"); return 2

    stats = {"scanned": 0, "canonical": 0, "rewritten": 0, "refused": 0, "errors": 0}
    samples, refused_rows = [], []
    offset, pages_done = 0, 0
    while True:
        rows = pipe(base, token,
                    'SELECT "key", payload FROM nhentai_cache '
                    'WHERE "key" LIKE \'gallery:%\' OR "key" LIKE \'search:%\' '
                    'ORDER BY "key" LIMIT ? OFFSET ?', [PAGE, offset])
        if not rows:
            break
        pages_done += 1
        for r in rows:
            key, raw = r.get("key"), r.get("payload")
            stats["scanned"] += 1
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                stats["errors"] += 1
                samples.append((key, "UNPARSEABLE payload JSON"))
                continue
            ok, canon = normalize_for_write(key, payload, source="migration")
            if not ok:
                stats["refused"] += 1
                refused_rows.append(key)
                continue
            if canon == payload:
                stats["canonical"] += 1
                continue
            if len(samples) < 10:
                samples.append((key, f"would rewrite ({type(payload).__name__} -> canonical)"))
            if a.apply:
                try:
                    pipe(base, token,
                         'UPDATE nhentai_cache SET payload = ? WHERE "key" = ?',
                         [json.dumps(canon, separators=(",", ":"), default=str), key])
                    stats["rewritten"] += 1
                except Exception as e:
                    stats["errors"] += 1
                    print(f"  !! write failed {key}: {e}")
            else:
                stats["rewritten"] += 1
        offset += PAGE
        if max_pages and pages_done >= max_pages:
            print(f"(bounded run: stopped after {max_pages} pages)")
            break
        time.sleep(0.2)

    mode = "APPLY" if a.apply else "DRY RUN"
    print(f"\n=== migration {mode} ===")
    print(f"scanned:   {stats['scanned']}")
    print(f"canonical: {stats['canonical']}  (already fine, untouched)")
    print(f"{'rewritten' if a.apply else 'would rewrite'}: {stats['rewritten']}")
    print(f"refused:   {stats['refused']}  (validation failed — SKIPPED)")
    print(f"errors:    {stats['errors']}")
    if refused_rows:
        print("refused keys (first 20):", refused_rows[:20])
    for k, note in samples:
        print(f"  · {k}: {note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
