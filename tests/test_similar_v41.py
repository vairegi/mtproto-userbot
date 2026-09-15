"""v4.1: /similar on|off toggle + leaderboard own-rank + corrected user menu."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo, posting
from app.handlers import member_cmds
import app.main as main_mod


# ---- similar prefs ----
@pytest.fixture
def fake_settings(monkeypatch):
    store = {}
    async def _gsj(key, default=None): return store.get(key, default)
    async def _ssj(key, val): store[key] = val
    monkeypatch.setattr(repo, "get_setting_json", _gsj)
    monkeypatch.setattr(repo, "set_setting_json", _ssj)
    monkeypatch.setattr(repo, "_mongo", lambda: False)
    return store


def test_similar_pref_default_on(fake_settings):
    assert asyncio.run(repo.get_similar_pref(42)) is True


def test_similar_pref_off_then_on(fake_settings):
    asyncio.run(repo.set_similar_pref(42, False))
    assert asyncio.run(repo.get_similar_pref(42)) is False
    asyncio.run(repo.set_similar_pref(42, True))
    assert asyncio.run(repo.get_similar_pref(42)) is True
    assert 42 not in fake_settings["similar_prefs"]["off"]  # clean removal


def test_send_similar_respects_optout(monkeypatch):
    async def _pref(uid): return False
    monkeypatch.setattr(posting.repo, "get_similar_pref", _pref)
    called = {"n": 0}
    async def _sims(cover, limit=5):
        called["n"] += 1
        return []
    monkeypatch.setattr("app.services.recommend.similar_covers", _sims)
    asyncio.run(posting._send_similar(SimpleNamespace(), 42, {"id": 1, "caption": "x"}, []))
    assert called["n"] == 0  # ranking never runs for opted-out users


def test_similar_command_toggle(fake_settings):
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    m = SimpleNamespace(text="/similar off", from_user=SimpleNamespace(id=7), reply=_reply)
    asyncio.run(member_cmds.cmd_similar(m))
    assert "OFF" in replies[0]
    m2 = SimpleNamespace(text="/similar", from_user=SimpleNamespace(id=7), reply=_reply)
    asyncio.run(member_cmds.cmd_similar(m2))
    assert "OFF ❌" in replies[1]
    m3 = SimpleNamespace(text="/similar on", from_user=SimpleNamespace(id=7), reply=_reply)
    asyncio.run(member_cmds.cmd_similar(m3))
    assert "ON" in replies[2]


# ---- leaderboard own rank ----
def _lb_env(monkeypatch, rows, caller_rank, names):
    async def _tfw(limit=10): return rows[:limit]
    async def _rank(uid): return caller_rank
    async def _dir(uids): return {u: names.get(u, {"username": None, "first_name": None}) for u in uids}
    async def _upsert(uid, uname, fname): pass
    monkeypatch.setattr(member_cmds.repo, "top_fetchers_week", _tfw)
    monkeypatch.setattr(member_cmds.repo, "fetch_rank_week", _rank)
    monkeypatch.setattr(member_cmds.repo, "get_directory_users", _dir)
    monkeypatch.setattr(member_cmds.repo, "upsert_directory_user", _upsert)


class _Bot:
    async def get_chat(self, uid): raise Exception("nope")


def _msg(uid):
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    return SimpleNamespace(text="/leaderboard", from_user=SimpleNamespace(id=uid), reply=_reply), replies


def test_leaderboard_shows_own_rank(monkeypatch):
    rows = [{"user_id": i, "fetches": 100 - i} for i in range(1, 11)]
    _lb_env(monkeypatch, rows, (52, 3), {i: {"username": f"user{i}"} for i in range(1, 11)} | {999: {"first_name": "Me"}})
    m, replies = _msg(999)
    asyncio.run(member_cmds.cmd_leaderboard(m, _Bot()))
    out = replies[0]
    assert "🥇" in out and "(You)" in out and "52." in out
    assert "— <b>3</b> files (You)" in out


def test_leaderboard_no_own_line_when_in_top(monkeypatch):
    rows = [{"user_id": i, "fetches": 100 - i} for i in range(1, 11)]
    _lb_env(monkeypatch, rows, (1, 99), {i: {"username": f"user{i}"} for i in range(1, 11)})
    m, replies = _msg(1)  # caller IS rank 1
    asyncio.run(member_cmds.cmd_leaderboard(m, _Bot()))
    assert "(You)" not in replies[0]


def test_leaderboard_no_own_line_when_zero_fetches(monkeypatch):
    rows = [{"user_id": 1, "fetches": 10}]
    _lb_env(monkeypatch, rows, (0, 0), {1: {"username": "top"}})
    m, replies = _msg(555)
    asyncio.run(member_cmds.cmd_leaderboard(m, _Bot()))
    assert "(You)" not in replies[0]


def test_fetch_rank_week_math(monkeypatch):
    async def _counts(): return {"1": 100, "2": 100, "3": 50, "4": 3, "5": 1}
    monkeypatch.setattr(repo, "_fetch_week_counts", _counts)
    assert asyncio.run(repo.fetch_rank_week(4)) == (4, 3)   # 3 users above
    assert asyncio.run(repo.fetch_rank_week(1)) == (1, 100) # tie at top shares rank 1
    assert asyncio.run(repo.fetch_rank_week(999)) == (0, 0) # no fetches


# ---- menu ----
def test_user_menu_contains_only_real_commands():
    cmds = [c.command for c in main_mod.USER_MENU]
    assert cmds == ["start", "help", "whoami", "favs", "rfavs", "mystats", "leaderboard", "similar"]
    for ghost in ("streak", "random", "recent", "fixnumbers", "backfill_check"):  # v4.2: mystats is real now
        assert ghost not in cmds
