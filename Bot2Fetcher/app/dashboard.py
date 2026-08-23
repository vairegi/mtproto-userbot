"""
dashboard.py — v12.40j detailed live dashboard for the log channel.

v12.40j: resolve peer via get_dialogs() FIRST so the send/edit does not
raise PeerIdInvalidError on a fresh session. When resolve fails the
reason is logged loudly and the dashboard disables itself instead of
sitting silent forever.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("bot2fetcher.dashboard")

EDIT_EVERY_S = 30


def _fmt_uptime(secs: int) -> str:
    h, rem = divmod(int(secs), 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h{m:02d}m"


def _progress_bar(done: int, total: int, width: int = 14) -> str:
    if total <= 0:
        return "[" + "░" * width + "] 0%"
    filled = int(width * done / total)
    pct = int(100 * done / total)
    return "[" + "█" * filled + "░" * (width - filled) + f"] {pct}%"


def _global_card(stats, scan_info: dict, mongo_counts: dict) -> str:
    s = stats.snapshot()
    total_cache = scan_info.get("cache_total", 0)
    completed_db = mongo_counts.get("COMPLETED", 0) + mongo_counts.get("PARTIAL", 0)
    failed_db = sum(v for k, v in mongo_counts.items()
                    if isinstance(k, str) and k.startswith("FAILED"))
    remaining = max(0, total_cache - completed_db - s["claimed"])
    lines = [
        "🤖 **Bot2Fetcher — GLOBAL**",
        "",
        f"📡 Phase: {scan_info.get('phase', 'scanning')}",
        f"🗂 Turso cache: **{total_cache}** galleries",
        f"✅ In DB channel: **{completed_db}**",
        f"❌ Failed-before (Mongo): **{failed_db}**",
        f"📋 Remaining: **{remaining}**",
        f"📊 Progress: {_progress_bar(completed_db, total_cache)}",
        "",
        f"🔄 Cycles: {s['cycles']}   🎯 Claimed: {s['claimed']}",
        f"✅ Done: {s['completed']}   ❌ Failed: {s['failed']}   🧹 Dropped: {s['dropped']}",
        f"⏭ Already done: {s['skipped_done']}   🚫 Failed-before: {s['skipped_failed']}   🔒 Busy: {s['skipped_busy']}",
        f"⏱ Uptime: {_fmt_uptime(s['uptime_s'])}   🕒 {time.strftime('%H:%M:%S UTC', time.gmtime())}",
    ]
    return "\n".join(lines)


def _slot_card(idx: int, account: str, slot: dict) -> str:
    state = slot.get("state", "idle")
    state_emoji = {"idle": "😴", "working": "⚙️", "waiting_pdf": "⏳",
                   "posting": "📤", "resting": "⏸"}.get(state, "❓")
    lines = [
        f"🧵 **Slot {idx}** — @{account}",
        "",
        f"State: {state_emoji} {state}",
    ]
    cur = slot.get("current")
    if cur:
        lines += [
            f"🎯 Now: **#{cur['gid']}**",
            f"   📖 {cur.get('title', '')[:60]}",
            f"   📄 {cur.get('pages', 0)} pages · step: {cur.get('step', '')}",
        ]
    lines += [
        "",
        f"✅ {slot.get('completed', 0)}   ❌ {slot.get('failed', 0)}   🧹 {slot.get('dropped', 0)}   🚧 FloodWaits: {slot.get('floodwaits', 0)}",
    ]
    recent = slot.get("recent") or []
    if recent:
        lines.append("")
        lines.append("Last jobs:")
        for r in recent[-3:]:
            lines.append(f"  {r}")
    lines.append(f"🕒 {time.strftime('%H:%M:%S UTC', time.gmtime())}")
    return "\n".join(lines)


class Dashboard:
    def __init__(self, settings, turso, stats):
        self.s = settings
        self.turso = turso
        self.stats = stats
        self.msg_ids: dict = {}
        self.channel = None
        self.scan_info: dict = {"phase": "booting", "cache_total": 0}
        self.slots: dict = {}
        self.accounts: dict = {}
        self.galleries = None

    def set_account(self, idx: int, username: str) -> None:
        self.accounts[idx] = username
        self.slots.setdefault(idx, {"state": "idle", "completed": 0,
                                    "failed": 0, "dropped": 0,
                                    "floodwaits": 0, "recent": []})

    def set_scan_info(self, **kw) -> None:
        self.scan_info.update(kw)

    def slot_state(self, idx: int, state: str, gid: str = "",
                   title: str = "", pages: int = 0, step: str = "") -> None:
        d = self.slots.setdefault(idx, {"state": "idle", "completed": 0,
                                        "failed": 0, "dropped": 0,
                                        "floodwaits": 0, "recent": []})
        d["state"] = state
        if gid:
            d["current"] = {"gid": gid, "title": title, "pages": pages, "step": step}
        elif state == "idle":
            d.pop("current", None)
        elif "current" in d:
            d["current"]["step"] = step or d["current"].get("step", "")

    def slot_event(self, idx: int, kind: str, gid: str) -> None:
        d = self.slots.setdefault(idx, {"state": "idle", "completed": 0,
                                        "failed": 0, "dropped": 0,
                                        "floodwaits": 0, "recent": []})
        if kind == "floodwait":
            d["floodwaits"] += 1
            return
        if kind in ("completed", "failed", "dropped"):
            d[kind] += 1
            mark = {"completed": "✅", "failed": "❌", "dropped": "🧹"}[kind]
            d["recent"].append(f"{mark} #{gid}")
            d["recent"] = d["recent"][-3:]

    async def _resolve_channel(self, client) -> bool:
        """Warm the peer cache, then resolve LOG_CHANNEL_ID via
        get_input_entity so the very first send does not
        PeerIdInvalidError. Return True on success."""
        raw = self.s.log_channel_id
        try:
            # Warm the peer cache — critical for freshly-imported sessions.
            async for _ in client.iter_dialogs(limit=200):
                pass
        except Exception as e:
            log.warning("📊 iter_dialogs failed while warming peer cache: %s", e)
        try:
            key = int(raw) if raw.lstrip("-").isdigit() else raw
            self.channel = await client.get_input_entity(key)
            log.info("📊 log channel resolved: %r", raw)
            return True
        except Exception as e:
            log.error("📊 dashboard: cannot resolve LOG_CHANNEL_ID %r — %s. "
                      "DASHBOARD DISABLED. Fix: make sure the userbot is a "
                      "MEMBER of the channel (admin is fine, but membership "
                      "is what registers the peer). If you just added it, "
                      "restart the Render service so the session warms up.",
                      raw, e)
            self.channel = None
            return False

    async def run(self, fetcher) -> None:
        if not self.s.log_channel_id:
            log.info("📭 dashboard disabled (LOG_CHANNEL_ID unset)")
            return
        client = fetcher.clients[0]
        if not await self._resolve_channel(client):
            return
        saved = await self.turso.get_state("_dashboard")
        if saved:
            self.msg_ids = {k: int(v) for k, v in saved.items()
                            if isinstance(v, int) or (isinstance(v, str) and v.isdigit())}
        log.info("📊 dashboard started (msg ids: %s)", self.msg_ids or "will post new")

        while not fetcher._stop.is_set():
            try:
                await self._tick(client, len(fetcher.clients))
            except Exception as e:
                log.warning("📊 dashboard tick failed: %s", e)
            await asyncio.sleep(EDIT_EVERY_S)

    async def _tick(self, client, n_slots: int) -> None:
        mongo_counts = {}
        if self.galleries is not None:
            try:
                mongo_counts = await asyncio.get_event_loop().run_in_executor(
                    None, self.galleries.count_by_status)
            except Exception:
                mongo_counts = {}
        global_text = _global_card(self.stats, self.scan_info, mongo_counts)
        await self._upsert(client, "global", global_text)
        for idx in range(1, n_slots + 1):
            acct = self.accounts.get(idx, "?")
            slot = self.slots.get(idx, {"state": "idle", "completed": 0,
                                        "failed": 0, "dropped": 0,
                                        "floodwaits": 0, "recent": []})
            await self._upsert(client, f"slot{idx}", _slot_card(idx, acct, slot))

    async def _upsert(self, client, key: str, text: str) -> None:
        mid = self.msg_ids.get(key, 0)
        try:
            if mid:
                await client.edit_message(self.channel, mid, text)
                return
        except Exception as e:
            log.warning("📊 edit %s failed (%s) — reposting", key, e)
            self.msg_ids[key] = 0
        try:
            msg = await client.send_message(self.channel, text)
            self.msg_ids[key] = int(msg.id)
            await self.turso.put_state("_dashboard", dict(self.msg_ids))
        except Exception as e:
            log.warning("📊 send %s failed: %s", key, e)
