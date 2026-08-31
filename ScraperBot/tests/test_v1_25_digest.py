"""v1.25 unit tests — page caps, per-page tracking, digest report.

Run from repo root:  python3 ScraperBot/tests/test_v1_25_digest.py
"""
from __future__ import annotations
import asyncio, pathlib, sys, time, types

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
SB = ROOT / "ScraperBot"
sys.path.insert(0, str(SB))

# ---- stubs ---------------------------------------------------------------
class FakeState:
    def __init__(self):
        self.store = {}
fake = FakeState()

settings = types.SimpleNamespace(
    admin_user_ids=[111, 222],
    bot_token="tok",
    digest_enabled=True,
    digest_time_ist="10:00",
    details_per_tick=5,
    details_rest_sec=0,
)

app_pkg = types.ModuleType("app"); app_pkg.__path__ = [str(SB / "app")]
sys.modules["app"] = app_pkg
cfg = types.ModuleType("app.config"); cfg.settings = settings
sys.modules["app.config"] = cfg
mc = types.ModuleType("app.mongo_client")
mc.state_get = lambda k, d=None: fake.store.get(k, d)
mc.state_set = lambda k, v: fake.store.__setitem__(k, v)
mc.is_paused = lambda: False
mc.hint_pop_gids = lambda n: []
sys.modules["app.mongo_client"] = mc

import importlib
dash = importlib.import_module("app.services.channel_dashboard")
dig = importlib.import_module("app.services.discovery_digest")
det = importlib.import_module("app.services.details_sweeper")

# ---- config defaults ------------------------------------------------------
cfg_mod_src = (SB / "app" / "config.py").read_text()
assert '_env_int("LIST_MAX_PAGES", 20)' in cfg_mod_src, "LIST_MAX_PAGES default not 20"
assert '_env_int("LIST_TAG_MAX_PAGES", 5)' in cfg_mod_src, "LIST_TAG_MAX_PAGES default not 5"
print("page-cap defaults OK (20 / 5)")

# ---- digest next-fire math ------------------------------------------------
# freeze now at 09:30 IST → next fire must be today 10:00 IST
from datetime import datetime, timezone, timedelta
IST = dig.IST
t = datetime(2026, 8, 31, 9, 30, tzinfo=IST).timestamp()
nxt = dig._next_fire_epoch(t)
assert datetime.fromtimestamp(nxt, tz=IST).hour == 10
assert abs((nxt - t) - 1800) < 2
# 10:05 IST → tomorrow 10:00
t2 = datetime(2026, 8, 31, 10, 5, tzinfo=IST).timestamp()
nxt2 = dig._next_fire_epoch(t2)
d = datetime.fromtimestamp(nxt2, tz=IST)
assert d.day == 1 and d.hour == 10 and d.minute == 0
# exact 10:00:00 → tomorrow
t3 = datetime(2026, 8, 31, 10, 0, 0, tzinfo=IST).timestamp()
assert datetime.fromtimestamp(dig._next_fire_epoch(t3), tz=IST).day == 1
print("digest next-fire math OK")

# ---- per-page ring → digest report ----------------------------------------
now = time.time()
dash._save_counters({"per_page_new": [
    ["popular-today", 1, now - 3600],
    ["popular-today", 1, now - 3000],
    ["popular-today", 3, now - 2000],
    ["tag:incest", 2, now - 1000],
    ["date", 1, now - 90000],        # >24h old — must be excluded
    ["broken"],                      # malformed — skipped
]})
text, total = dig.build_report(now)
assert total == 4, total
assert "popular-today" in text and "page 1: 2" in text and "page 3: 1" in text
assert "tag:incest" in text and "page 2: 1" in text
assert "date" not in text  # expired entry excluded
print("digest report aggregation OK")

# empty ring → "no new" message
dash._save_counters({"per_page_new": []})
text, total = dig.build_report(now)
assert total == 0 and "No new galleries" in text
print("digest empty case OK")

# ---- details_sweeper per-page recorder ------------------------------------
det._record_new_on_page("popular", 7)
det._record_new_on_page("tag:incest", 2)
c = dash._counters()
ring = c.get("per_page_new") or []
assert any(e[0] == "popular" and e[1] == 7 for e in ring)
assert any(e[0] == "tag:incest" and e[1] == 2 for e in ring)
print("details per-page recorder OK")

# ---- broadcast fan-out (mocked) -------------------------------------------
sent = []
tb = importlib.import_module("app.services.telegram_bot")
async def fake_send(chat_id, text):
    sent.append(chat_id)
    return {"ok": True}
tb.send_message = fake_send

async def one_shot():
    # simulate the send block: last-send guard + fan-out
    fake.store["discovery_digest_last_sent"] = 0
    text, total = dig.build_report(time.time())
    for uid in settings.admin_user_ids:
        r = await tb.send_message(int(uid), text)
        assert r["ok"]
    mc.state_set("discovery_digest_last_sent", time.time())
asyncio.run(one_shot())
assert sent == [111, 222], sent
assert fake.store["discovery_digest_last_sent"] > 0
print("broadcast fan-out OK (2 admins)")

print("\nALL v1.25 TESTS PASS")
