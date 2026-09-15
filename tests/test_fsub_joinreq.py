"""v3.3.1: fsub gate accepts recorded join requests (private approval channels),
and Retry->delivery never re-blocks a recorded requester."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import fsub
from app.handlers import fsub_cmds


class FakeBot:
    def __init__(self, behavior):
        self.behavior = behavior
        self.sent = []
    async def get_chat_member(self, chat_id, user_id):
        if self.behavior == "notfound":
            raise Exception("Bad Request: user not found")
        if self.behavior == "participant":
            raise Exception("Bad Request: user not a participant")
        if self.behavior == "left":
            return SimpleNamespace(status="left")
        return SimpleNamespace(status="member")
    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return SimpleNamespace(message_id=1)


def _patch(monkeypatch, pending):
    async def _list():
        return [{"chat_id": -100777, "link": "https://t.me/+AbC", "title": "Priv"}]
    async def _has(cid, uid):
        return pending
    monkeypatch.setattr(fsub, "list_fsub", _list)
    monkeypatch.setattr(fsub.repo, "has_fsub_request", _has)


def test_notfound_with_pending_passes(monkeypatch):
    _patch(monkeypatch, True)
    assert asyncio.run(fsub.unjoined_channels(FakeBot("notfound"), 42)) == []


def test_participant_phrasing_with_pending_passes(monkeypatch):
    _patch(monkeypatch, True)
    assert asyncio.run(fsub.unjoined_channels(FakeBot("participant"), 42)) == []


def test_notfound_without_pending_blocks(monkeypatch):
    _patch(monkeypatch, False)
    assert len(asyncio.run(fsub.unjoined_channels(FakeBot("notfound"), 42))) == 1


def test_left_with_pending_passes(monkeypatch):
    _patch(monkeypatch, True)
    assert asyncio.run(fsub.unjoined_channels(FakeBot("left"), 42)) == []


def test_left_without_pending_blocks(monkeypatch):
    _patch(monkeypatch, False)
    assert len(asyncio.run(fsub.unjoined_channels(FakeBot("left"), 42))) == 1


def test_member_always_passes(monkeypatch):
    _patch(monkeypatch, False)
    assert asyncio.run(fsub.unjoined_channels(FakeBot("member"), 42)) == []


def test_gate_called_twice_requester_passes_both(monkeypatch):
    """Retry re-check AND delivery re-check must BOTH pass (no early purge)."""
    _patch(monkeypatch, True)
    bot = FakeBot("notfound")
    assert asyncio.run(fsub.unjoined_channels(bot, 42)) == []
    assert asyncio.run(fsub.unjoined_channels(bot, 42)) == []
    assert asyncio.run(fsub.check_or_gate(bot, 42, "c1")) is True
    assert bot.sent == []


def test_join_request_event_records_only_fsub_channels(monkeypatch):
    recorded = []
    async def _list():
        return [{"chat_id": -100777, "link": "https://t.me/+AbC", "title": "Priv"}]
    async def _add(cid, uid):
        recorded.append((cid, uid))
    monkeypatch.setattr(fsub_cmds.fsub, "list_fsub", _list)
    monkeypatch.setattr(fsub_cmds.repo, "add_fsub_request", _add)
    asyncio.run(fsub_cmds.on_fsub_join_request(
        SimpleNamespace(chat=SimpleNamespace(id=-100777), from_user=SimpleNamespace(id=555))))
    assert recorded == [(-100777, 555)]
    asyncio.run(fsub_cmds.on_fsub_join_request(
        SimpleNamespace(chat=SimpleNamespace(id=-100999), from_user=SimpleNamespace(id=556))))
    assert recorded == [(-100777, 555)]


def test_fsub_sync_imports(monkeypatch):
    import app.services.userbot as ub_mod
    replies, added = [], []
    async def _false(_m): return False
    async def _fetch(cid): return [11, 22, 33]
    async def _add(cid, uid): added.append((cid, uid))
    monkeypatch.setattr(fsub_cmds, "_reject_non_admin", _false)
    monkeypatch.setattr(ub_mod, "fetch_join_requests", _fetch)
    monkeypatch.setattr(fsub_cmds.repo, "add_fsub_request", _add)
    async def _reply(t, **kw): replies.append(str(t))
    m = SimpleNamespace(text="/fsub_sync -100777", reply=_reply)
    asyncio.run(fsub_cmds.cmd_fsub_sync(m))
    assert added == [(-100777, 11), (-100777, 22), (-100777, 33)]
    assert any("Imported" in r and "3" in r for r in replies)


# v4.2: the 15-minute membership cache must not leak state between tests.
import pytest as _pytest
from app.services import fsub as _fsub_mod


@_pytest.fixture(autouse=True)
def _clear_fsub_member_cache():
    _fsub_mod._member_cache.clear()
