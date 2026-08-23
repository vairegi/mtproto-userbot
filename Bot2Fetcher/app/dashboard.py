"""
dashboard.py — v12.40k Bot API log dashboard.

Instead of asking a userbot to post/edit into the log channel (which
was flaky because Telethon's peer table is per-session and had trouble
resolving freshly-added private channels), the dashboard now speaks the
Bot API directly via httpx. A dedicated Bot account (env var
BOT_2_PDF_FECTHER — kept verbatim) is admin in the log channel and owns
ONE merged status message that is edited in place every 30 s.

Layout — one Markdown message with GLOBAL + every slot in sections.

If BOT_2_PDF_FECTHER is unset, dashboard is disabled and we log why.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

import httpx

log = logging.getLogger("bot2fetcher.dashboard")

EDIT_EVERY_S = 30
_API = "https://api.telegram.org"

# Markdown special chars that must NOT be inside our field values.
# We escape them so titles like `[Foo] Bar_Baz` don't break rendering.
_MD_ESC = str.maketrans({c: "\\" + c for c in r"_*[]()~`>#+-=|{}.!"})


def _md(s: str) -> str:
    return (s or "").translate(_MD_ESC)


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


class LogBot:
    """Tiny async Bot API client via httpx."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self._me: Optional[Dict[str, Any]] = None

    async def _call(self, method: str, payload: dict) -> Optional[dict]:
        url = f"{_API}/bot{self.token}/{method}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as h:
                r = await h.post(url, json=payload)
                data = r.json()
        except Exception as e:
            log.warning("📊 bot api %s: %s", method, e)
            return None
        if not data.get("ok"):
            desc = data.get("description", "")
            # "message is not modified" is harmless — edit called with same text.
            if "message is not modified" in desc.lower():
                return {"ok": True, "no_op": True}
            log.warning("📊 bot api %s failed: %s", method, desc)
            return None
        return data.get("result")

    async def check(self) -> bool:
        me = await self._call("getMe", {})
        if not me:
            return False
        self._me = me
        log.info("📊 log bot ready: @%s (id=%s)",
                 me.get("username"), me.get("id"))
        return True

    async def send_markdown(self, text: str) -> Optional[int]:
        r = await self._call("sendMessage", {
            "chat_id": self.chat_id, "text": text,
            "parse_mode": "Markdown", "disable_web_page_preview": True,
        })
        if r and "message_id" in r:
            return int(r["message_id"])
        return None

    async def edit_markdown(self, msg_id: int, text: str) -> bool:
        r = await self._call("editMessageText", {
            "chat_id": self.chat_id, "message_id": int(msg_id),
            "text": text, "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        })
        return r is not None


def _build_message(stats, scan_info: dict, mongo_counts: dict,
                   slots: Dict[int, dict], accounts: Dict[int, str]) -> str:
    s = stats.snapshot()
    total_cache = scan_info.get("cache_total", 0)
    completed_db = mongo_counts.get("COMPLETED", 0) + mongo_counts.get("PARTIAL", 0)
    failed_db = sum(v for k, v in mongo_counts.items()
                    if isinstance(k, str) and k.startswith("FAILED"))
    remaining = max(0, total_cache - completed_db - s["claimed"])

    lines = [
        "🤖 **Bot2Fetcher — Live**",
        "",
        f"📡 Phase: `{_md(scan_info.get('phase', 'scanning'))}`",
        f"🗂 Turso cache: **{total_cache}** galleries",
        f"✅ In DB channel: **{completed_db}**",
        f"❌ Failed-before (Mongo): **{failed_db}**",
        f"📋 Remaining: **{remaining}**",
        f"📊 Progress: `{_progress_bar(completed_db, total_cache)}`",
        "",
        f"🔄 Cycles: **{s['cycles']}**   🎯 Claimed: **{s['claimed']}**",
        f"✅ Done: **{s['completed']}**   ❌ Failed: **{s['failed']}**   🧹 Dropped: **{s['dropped']}**",
        f"⏭ Already done: **{s['skipped_done']}**   🚫 Failed-before: **{s['skipped_failed']}**   🔒 Busy: **{s['skipped_busy']}**",
        f"⏱ Uptime: **{_fmt_uptime(s['uptime_s'])}**   🕒 `{time.strftime('%H:%M:%S UTC', time.gmtime())}`",
        "",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    for idx in sorted(slots.keys()):
        slot = slots[idx]
        state = slot.get("state", "idle")
        state_emoji = {"idle": "😴", "working": "⚙️", "waiting_pdf": "⏳",
                       "posting": "📤", "resting": "⏸"}.get(state, "❓")
        acct = accounts.get(idx, "?")
        lines += [
            "",
            f"🧵 **Slot {idx}** — @{_md(acct)}",
            f"State: {state_emoji} `{state}`",
        ]
        cur = slot.get("current")
        if cur:
            title = (cur.get("title") or "")[:60]
            lines += [
                f"🎯 Now: **#{_md(str(cur['gid']))}**",
                f"   📖 _{_md(title)}_",
                f"   📄 {cur.get('pages', 0)} pages · step: `{_md(cur.get('step', ''))}`",
            ]
        lines.append(
            f"✅ **{slot.get('completed', 0)}**   ❌ **{slot.get('failed', 0)}**   "
            f"🧹 **{slot.get('dropped', 0)}**   🚧 FloodWaits: **{slot.get('floodwaits', 0)}**"
        )
        recent = slot.get("recent") or []
        if recent:
            lines.append("Last jobs:")
            for r in recent[-3:]:
                lines.append(f"  `{_md(r)}`")

    return "\n".join(lines)


class Dashboard:
    def __init__(self, settings, turso, stats):
        self.s = settings
        self.turso = turso
        self.stats = stats
        self.msg_id: int = 0
        self.scan_info: dict = {"phase": "booting", "cache_total": 0}
        self.slots: Dict[int, dict] = {}
        self.accounts: Dict[int, str] = {}
        self.galleries = None
        self.bot: Optional[LogBot] = None

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

    async def run(self, fetcher) -> None:
        if not self.s.log_channel_id:
            log.info("📭 dashboard disabled (LOG_CHANNEL_ID unset)")
            return
        if not self.s.log_bot_token:
            log.error("📊 dashboard disabled — BOT_2_PDF_FECTHER (log bot "
                      "token) is not set. Create a Telegram bot via "
                      "@BotFather, add it as admin to the log channel, and "
                      "set BOT_2_PDF_FECTHER in Render.")
            return
        self.bot = LogBot(self.s.log_bot_token, self.s.log_channel_id)
        if not await self.bot.check():
            log.error("📊 dashboard disabled — BOT_2_PDF_FECTHER token "
                      "rejected by Telegram. Double-check the token value.")
            return

        saved = await self.turso.get_state("_dashboard")
        if saved:
            v = saved.get("bot_msg_id") or saved.get("global")
            try:
                self.msg_id = int(v) if v else 0
            except (TypeError, ValueError):
                self.msg_id = 0
        log.info("📊 dashboard started via Bot API (msg id: %s)",
                 self.msg_id or "will post new")

        while not fetcher._stop.is_set():
            try:
                await self._tick(len(fetcher.clients))
            except Exception as e:
                log.warning("📊 dashboard tick failed: %s", e)
            await asyncio.sleep(EDIT_EVERY_S)

    async def _tick(self, n_slots: int) -> None:
        mongo_counts = {}
        if self.galleries is not None:
            try:
                mongo_counts = await asyncio.get_event_loop().run_in_executor(
                    None, self.galleries.count_by_status)
            except Exception:
                mongo_counts = {}
        for idx in range(1, n_slots + 1):
            self.slots.setdefault(idx, {"state": "idle", "completed": 0,
                                        "failed": 0, "dropped": 0,
                                        "floodwaits": 0, "recent": []})
        text = _build_message(self.stats, self.scan_info, mongo_counts,
                              self.slots, self.accounts)
        # Telegram caps at 4096 chars.
        if len(text) > 4000:
            text = text[:3990] + "\n…"

        if self.msg_id:
            if await self.bot.edit_markdown(self.msg_id, text):
                return
            log.info("📊 edit failed — reposting")
            self.msg_id = 0
        new_id = await self.bot.send_markdown(text)
        if new_id:
            self.msg_id = new_id
            await self.turso.put_state("_dashboard", {"bot_msg_id": self.msg_id})
