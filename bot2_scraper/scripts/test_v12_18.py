#!/usr/bin/env python3
"""
test_v12_18.py — browserless verification of the v12.18 fixes.

  TEST 1  scraper_bridge rate-limit dicts stay bounded under writes:
          after > cap writes with expired bans, sweep shrinks the dict;
          after > cap live writes, oldest-first eviction keeps it ≤ cap.
  TEST 2  prefetch_cron priority queue: push dedupes, persists via the
          (stubbed) Mongo settings store, pops drain+sort-guarded.
  TEST 3  PREFETCH_MAX_PAGES default is 20 (was 10).
  TEST 4  userbot.build_client passes flood_sleep_threshold and
          request_retries to TelegramClient (env-overridable).

Run:  python3 scripts/test_v12_18.py     (exit 0 = all pass)
"""
import os
import sys
import time
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

failures = 0


def check(name, cond, extra=""):
    global failures
    if cond:
        print(f"  ok    {name}")
    else:
        print(f"  FAIL  {name}{' — ' + str(extra) if extra else ''}")
        failures += 1


# ---------------------------------------------------------------------------
# TEST 1 — rate-limit dicts bounded
# ---------------------------------------------------------------------------
print("\nTEST 1 — _RATE_LIMIT_CACHE / _RATE_LIMIT_STRIKES stay bounded")
from miniapp.backend.app.services import scraper_bridge as sb

sb._RATE_LIMIT_CACHE.clear()
sb._RATE_LIMIT_STRIKES.clear()

cap = sb._RL_CACHE_MAX_ENTRIES
check("cap default is 512", cap == 512, cap)

# 1a: 600 EXPIRED bans → sweep should shrink the dict far below cap
now = time.time()
sb._rl_writes_since_sweep = 0
for i in range(cap + 88):
    sb._rl_cache_set(("search", f"q{i}", "popular", i), now - 10)  # expired
check("expired-only writes swept below cap",
      len(sb._RATE_LIMIT_CACHE) < cap, len(sb._RATE_LIMIT_CACHE))

# 1b: 600 LIVE bans → cap enforced by oldest-first eviction
sb._RATE_LIMIT_CACHE.clear()
sb._RATE_LIMIT_STRIKES.clear()
sb._rl_writes_since_sweep = 0
for i in range(cap + 100):
    sb._rl_cache_set(("search", f"live{i}", "popular", i), now + 3600)
check("live writes capped at 512", len(sb._RATE_LIMIT_CACHE) <= cap,
      len(sb._RATE_LIMIT_CACHE))
check("oldest key evicted", ("search", "live0", "popular", 0) not in sb._RATE_LIMIT_CACHE)
check("newest key kept", ("search", f"live{cap+99}", "popular", cap + 99) in sb._RATE_LIMIT_CACHE)

# 1c: strikes dict capped too
sb._RATE_LIMIT_STRIKES.clear()
for i in range(cap + 50):
    sb._rl_strikes_set(("k", i), i)
check("strikes capped at 512", len(sb._RATE_LIMIT_STRIKES) <= cap,
      len(sb._RATE_LIMIT_STRIKES))

# 1d: backoff still ramps through the setter path
sb._RATE_LIMIT_STRIKES.clear()
key = ("search", "q", "popular", 1)
d1 = sb._rate_limit_backoff_sec(key, None)
d2 = sb._rate_limit_backoff_sec(key, None)
check("backoff ramps 60 -> 120", d1 == 60 and d2 == 120, (d1, d2))

# ---------------------------------------------------------------------------
# TEST 2 — prefetch priority queue (stubbed Mongo)
# ---------------------------------------------------------------------------
print("\nTEST 2 — prefetch_cron priority queue")
from miniapp.backend.app.services import prefetch_cron as pc

_store = {}


def _fake_set(key, value):
    _store[key] = value


def _fake_get(key, default=None):
    return _store.get(key, default)


with mock.patch.object(pc, "_db_set", side_effect=_fake_set), \
     mock.patch.object(pc, "_db_get", side_effect=_fake_get):
    _store.clear()
    pc._priority_push("popular", 5)
    pc._priority_push("popular", 5)   # duplicate — must dedupe
    pc._priority_push("date", 3)
    check("queue has 2 entries after dedupe", len(_store[pc._PERSIST_KEY]) == 2)

    out = pc._priority_pop_all()
    check("pop drains queue", out == [("popular", 5), ("date", 3)], out)
    check("queue cleared after pop", _store.get(pc._PERSIST_KEY) == [])

    # sort/page guard: junk entries are dropped on pop
    _store[pc._PERSIST_KEY] = [["popular", 7], ["bogus-sort", 1],
                               ["popular", 999], "garbage"]
    out = pc._priority_pop_all()
    check("pop drops invalid entries", out == [("popular", 7)], out)

# ---------------------------------------------------------------------------
# TEST 3 — PREFETCH_MAX_PAGES default raised
# ---------------------------------------------------------------------------
print("\nTEST 3 — PREFETCH_MAX_PAGES default")
check("PREFETCH_MAX_PAGES default is 20", pc.PREFETCH_MAX_PAGES == 20,
      pc.PREFETCH_MAX_PAGES)

# ---------------------------------------------------------------------------
# TEST 4 — userbot flood safeguards
# ---------------------------------------------------------------------------
print("\nTEST 4 — userbot.build_client Telethon knobs")
os.environ.pop("TELETHON_FLOOD_SLEEP_SEC", None)
os.environ.pop("TELETHON_REQUEST_RETRIES", None)
import importlib
import userbot
importlib.reload(userbot)
check("flood_sleep_threshold default 300", userbot._FLOOD_SLEEP_THRESHOLD == 300)
check("request_retries default 3", userbot._REQUEST_RETRIES == 3)

with mock.patch.object(userbot, "TelegramClient") as tc, \
     mock.patch.object(userbot, "StringSession") as ss, \
     mock.patch.object(userbot, "settings") as st:
    st.session_string = "x"   # value irrelevant — StringSession is stubbed
    st.api_id = 1
    st.api_hash = "h"
    userbot.build_client()
    kwargs = tc.call_args.kwargs
    check("flood_sleep_threshold passed", kwargs.get("flood_sleep_threshold") == 300,
          kwargs)
    check("request_retries passed", kwargs.get("request_retries") == 3, kwargs)

# env override
os.environ["TELETHON_FLOOD_SLEEP_SEC"] = "600"
importlib.reload(userbot)
check("env override honored", userbot._FLOOD_SLEEP_THRESHOLD == 600)
os.environ.pop("TELETHON_FLOOD_SLEEP_SEC", None)

print("\n" + ("ALL V12.18 TESTS PASSED" if failures == 0
              else f"{failures} V12.18 TEST(S) FAILED"))
sys.exit(0 if failures == 0 else 1)
