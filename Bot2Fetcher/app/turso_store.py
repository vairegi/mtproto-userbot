"""
turso_store.py — shared Turso access via the raw HTTP /v2/pipeline API.

v12.48 (sync-audit):
  * F1  list_recent_search_ids: accept the CANONICAL search payload shape —
        a LIST of card dicts (the shape Bot 1 has been writing since v1.16 and
        the shape the shared normalize_search_payload() emits). The v12.44
        implementation only looked at payload.get("result") on a dict, so
        every scan cycle extracted ZERO ids from real production rows and
        the "recent-first" reorder silently never fired. Live cache stat
        pre-fix: 3,950 search:q=* rows + 5,192 search:chip rows, ALL list-
        shaped — so the pre-fix producer was seeing 0 fresh gids from
        search pages and had to wait for Bot 1's details_sweeper to write
        a gallery:<id> before Bot 2 could ever pick it up.
  * F4  list_gallery_ids: incremental watermark-based scan
        (SELECT ... WHERE cached_at > :wm) with a periodic full rescan
        every FULL_RESCAN_EVERY cycles. The old unbounded SELECT walked
        11,325 rows every ~5 minutes = ~98M row-reads/month on a 500M/mo
        Turso free tier — right on the edge of exhausting quota just for
        Bot 2's producer. The watermark scan reads only the delta plus one
        full sweep per hour, cutting steady-state cost by two orders of
        magnitude while still guaranteeing recovery from missed rows.

Neither change touches the canonical Turso schema or the payload contract.
Bot 0 / Bot 1 read paths continue to see the same rows in the same shape.
"""
from __future__ import annotations

import os as _os
import sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ROOT = _os.path.abspath(_os.path.join(_HERE, "..", ".."))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)


import json
import logging
import time
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("bot2fetcher.turso")

STATE_TABLE = "bot2_fetch_state"
_MAX_SEARCH_ROWS = 200

# v12.48 (F4): incremental scan watermark tunables. Full-rescan cadence is
# deliberately generous — the producer already de-dupes against the known-
# done / known-failed sets, so a missed row simply gets picked up on the
# next full pass. Env-tunable so ops can retune without a redeploy.
_FULL_RESCAN_EVERY = max(1, int(_os.getenv("BOT2_LIST_FULL_RESCAN_EVERY", "12") or 12))
_WATERMARK_SLACK_SEC = max(0, int(_os.getenv("BOT2_LIST_WATERMARK_SLACK_S", "60") or 60))


def _normalise_url(raw: str) -> str:
    u = (raw or "").strip()
    if u.startswith("turso://"):
        u = "https://" + u[len("turso://"):]
    elif u.startswith("libsql://"):
        u = "https://" + u[len("libsql://"):]
    elif "://" not in u:
        u = "https://" + u
    return u.rstrip("/")


def _arg(v: Any) -> Dict[str, Any]:
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": "1" if v else "0"}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray)):
        import base64
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode()}
    return {"type": "text", "value": str(v)}


def _cell(c: Any) -> Any:
    if c is None:
        return None
    if not isinstance(c, dict):
        return c
    t = c.get("type")
    if t == "null":
        return None
    if t == "integer":
        try:
            return int(c.get("value") or 0)
        except (TypeError, ValueError):
            return 0
    if t == "float":
        try:
            return float(c.get("value") or 0)
        except (TypeError, ValueError):
            return 0.0
    if t == "blob":
        import base64
        try:
            return base64.b64decode(c.get("base64") or "")
        except Exception:
            return b""
    return c.get("value")


def _extract_ids_from_search_payload(payload: Any) -> List[str]:
    """v12.48 (F1): accept BOTH shapes a search: row may carry.

    The canonical contract (common/turso_cache/normalize.py) stores a LIST of
    card dicts under a search:* key. The pre-canonical raw-v2 shape was a
    DICT {"result": [...], "num_pages": N}. Either can still surface here
    for legacy rows written before v1.16, so we handle both defensively and
    also tolerate a nested {"result": [...]} inside a raw-v2 wrapper.

    Never raises. Returns ids as digit strings, preserving discovery order.
    """
    if payload is None:
        return []
    items: List[Any]
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        # legacy raw-v2 or defensive wrapper
        inner = payload.get("result")
        if isinstance(inner, list):
            items = inner
        else:
            return []
    else:
        return []

    out: List[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        # canonical cards carry "id"; some legacy rows expose "gallery_id"
        raw_id = it.get("id") or it.get("gallery_id") or it.get("media_id")
        if raw_id is None:
            continue
        gid = str(raw_id).strip()
        if gid.isdigit():
            out.append(gid)
    return out


class Turso:
    def __init__(self, url: str, token: str):
        self.base = _normalise_url(url)
        self.endpoint = self.base + "/v2/pipeline"
        self.token = token
        self._ready = False
        # v12.48 (F4): incremental scan state — process-local; a restart
        # simply performs one full scan before switching back to deltas.
        self._list_gallery_watermark: int = 0
        self._list_gallery_cycles: int = 0

    async def _pipeline(self, sql: str, args: Optional[list] = None) -> Optional[Dict[str, Any]]:
        # v12.41: delegate to the shared common.turso_http client so all
        # three bots share ONE /v2/pipeline implementation. Fall back to the
        # inline legacy path if the shared package isn't importable (e.g.
        # the file was dropped from a deploy).
        try:
            from common.turso_http import TursoHttpClient  # noqa: WPS433
        except Exception:
            TursoHttpClient = None  # type: ignore
        if TursoHttpClient is not None:
            client = getattr(self, "_shared", None)
            if client is None:
                client = TursoHttpClient(self.base, self.token, timeout=45.0)
                self._shared = client
            return await client.execute_raw(sql, list(args or []))

        # ---- legacy inline path (pre-v12.41), kept as fallback ----
        body = {
            "requests": [
                {"type": "execute",
                 "stmt": {"sql": sql,
                          "args": [_arg(a) for a in (args or [])]}},
                {"type": "close"},
            ]
        }
        headers = {"Authorization": f"Bearer {self.token}",
                   "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=45.0) as h:
                r = await h.post(self.endpoint, headers=headers, json=body)
            if r.status_code != 200:
                log.warning("🚨 turso HTTP %s: %s", r.status_code, r.text[:200])
                return None
            data = r.json()
        except Exception as e:
            log.warning("🚨 turso request failed (%s): %s", sql.split(None, 1)[0], e)
            return None
        results = data.get("results") or []
        if not results:
            log.warning("🚨 turso no results: %s", str(data)[:200])
            return None
        first = results[0]
        if first.get("type") != "ok":
            err = first.get("error") or first
            log.warning("🚨 turso stmt error (%s): %s",
                        sql.split(None, 1)[0], str(err)[:200])
            return None
        return (first.get("response") or {}).get("result") or {}

    async def execute(self, sql: str, args: Optional[list] = None) -> Optional[Dict[str, Any]]:
        result = await self._pipeline(sql, args)
        if result is None:
            return None
        cols = [c.get("name") for c in (result.get("cols") or [])]
        rows_out: List[Dict[str, Any]] = []
        for row in result.get("rows") or []:
            d: Dict[str, Any] = {}
            for i, cell in enumerate(row):
                key = cols[i] if i < len(cols) else str(i)
                d[key] = _cell(cell)
            rows_out.append(d)
        return {"columns": cols, "rows": rows_out}

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        await self.execute(
            f'CREATE TABLE IF NOT EXISTS {STATE_TABLE} ('
            '"key" TEXT PRIMARY KEY, payload TEXT NOT NULL, '
            'updated_at INTEGER NOT NULL DEFAULT 0)'
        )
        self._ready = True

    async def list_gallery_ids(self) -> List[Dict[str, Any]]:
        """v12.48 (F4): incremental watermark scan.

        Cycle 0 (or every _FULL_RESCAN_EVERY cycles) does a full walk of
        gallery:* keys and remembers max(cached_at); intermediate cycles
        read only rows whose cached_at is newer than the watermark minus
        a small slack window (in case of clock skew or overlapping writes).

        The old full-scan behavior remains as an automatic fallback if the
        watermark reads return an empty set for _FULL_RESCAN_EVERY cycles
        in a row (i.e. nothing new is being written — the producer should
        still see the full inventory once per rescan window).
        """
        self._list_gallery_cycles += 1
        full_sweep = (
            self._list_gallery_watermark == 0
            or (self._list_gallery_cycles % _FULL_RESCAN_EVERY) == 0
        )

        if full_sweep:
            sql = ('SELECT "key", cached_at FROM nhentai_cache '
                   'WHERE "key" LIKE \'gallery:%\'')
            args: List[Any] = []
        else:
            sql = ('SELECT "key", cached_at FROM nhentai_cache '
                   'WHERE "key" LIKE \'gallery:%\' AND cached_at >= ?')
            args = [max(0, self._list_gallery_watermark - _WATERMARK_SLACK_SEC)]

        result = await self.execute(sql, args)
        if not result:
            log.warning("🚨 list_gallery_ids: execute returned None "
                        "(full=%s, wm=%d)", full_sweep,
                        self._list_gallery_watermark)
            return []

        out: List[Dict[str, Any]] = []
        max_ca = self._list_gallery_watermark
        for r in result["rows"]:
            k = r.get("key") or ""
            gid = k.split(":", 1)[1] if ":" in k else ""
            if not gid.isdigit():
                continue
            try:
                ca = int(r.get("cached_at") or 0)
            except (TypeError, ValueError):
                ca = 0
            if ca > max_ca:
                max_ca = ca
            out.append({"gid": gid, "cached_at": ca})

        if max_ca > self._list_gallery_watermark:
            self._list_gallery_watermark = max_ca

        log.info(
            "📚 list_gallery_ids: %d galleries (mode=%s, wm=%d, cycle=%d)",
            len(out), "FULL" if full_sweep else "DELTA",
            self._list_gallery_watermark, self._list_gallery_cycles,
        )
        return out

    async def list_recent_search_ids(self) -> List[str]:
        """v12.48 (F1) — canonical-list-aware recent-first extraction.

        v12.44 (predecessor) already fixed the key-name filter (Recent =
        'search:date:*', not '*recent*'). The residual bug it left was that
        it only looked at payload.get("result") on a DICT payload — but
        since Bot 1's v1.16 normalise-on-write flip, EVERY search:* row on
        disk is a LIST of canonical card dicts, and the extractor returned
        zero ids from every one of them. This rewrite delegates parsing to
        _extract_ids_from_search_payload() which handles list, dict-with-
        result, and legacy raw-v2 payloads all in one place.

        Ordering: pages sorted ascending (page1 first) inside each pattern
        group, and Recent (`search:date:*`) is tried first; typed sort=date
        queries are queried second so tag-scoped 'recent' pages also feed
        the producer. Popular-today is the final fallback.
        """
        out: List[str] = []
        seen: set = set()

        async def _ids_from(patterns: List[str]) -> int:
            added_here = 0
            for pat in patterns:
                result = await self.execute(
                    'SELECT "key", payload FROM nhentai_cache '
                    'WHERE "key" LIKE ? LIMIT ?', [pat, _MAX_SEARCH_ROWS])
                if not result:
                    continue
                pages: List[tuple] = []
                for r in result["rows"]:
                    k = r.get("key") or ""
                    payload_raw = r.get("payload")
                    if not k or payload_raw is None:
                        continue
                    try:
                        if isinstance(payload_raw, (bytes, bytearray)):
                            payload_raw = payload_raw.decode("utf-8", "ignore")
                        payload = json.loads(payload_raw)
                    except Exception:
                        continue
                    page = 0
                    tail = k.rsplit("page", 1)[-1]
                    try:
                        page = int("".join(ch for ch in tail if ch.isdigit()) or 0)
                    except ValueError:
                        page = 0
                    pages.append((page, payload))
                pages.sort(key=lambda t: t[0])
                for _, payload in pages:
                    for gid in _extract_ids_from_search_payload(payload):
                        if gid not in seen:
                            seen.add(gid)
                            out.append(gid)
                            added_here += 1
            return added_here

        n_recent = await _ids_from(["search:date:%", "search:q=%|sort=date|%"])
        n_pop = 0
        if not n_recent:
            n_pop = await _ids_from(["search:popular-today:%"])
        log.info(
            "🔥 list_recent_search_ids: %d ids (recent=%d, popular=%d)",
            len(out), n_recent, n_pop,
        )
        return out

    async def get_gallery_row(self, gid: str) -> Optional[Dict[str, Any]]:
        result = await self.execute(
            'SELECT payload FROM nhentai_cache WHERE "key" = ?',
            [f"gallery:{gid}"])
        if not result or not result["rows"]:
            log.info("🔍 gallery:%s — no row in Turso", gid)
            return None
        raw = result["rows"][0].get("payload")
        if raw is None:
            log.warning("🔍 gallery:%s — row found but payload is NULL", gid)
            return None
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            return {"payload": json.loads(raw)}
        except Exception as e:
            log.warning("🔍 gallery:%s — payload JSON parse failed: %s", gid, e)
            return None

    async def put_state(self, key: str, payload: Dict[str, Any]) -> None:
        await self.ensure_schema()
        await self.execute(
            f'INSERT INTO {STATE_TABLE} ("key", payload, updated_at) '
            "VALUES (?, ?, ?) ON CONFLICT(\"key\") DO UPDATE SET "
            "payload=excluded.payload, updated_at=excluded.updated_at",
            [key, json.dumps(payload, separators=(",", ":")), int(time.time())],
        )

    async def get_state(self, key: str) -> Optional[Dict[str, Any]]:
        await self.ensure_schema()
        result = await self.execute(
            f'SELECT payload FROM {STATE_TABLE} WHERE "key" = ?', [key])
        if not result or not result["rows"]:
            return None
        raw = result["rows"][0].get("payload")
        if raw is None:
            return None
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "ignore")
            return json.loads(raw)
        except Exception:
            return None
