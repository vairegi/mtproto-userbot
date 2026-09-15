"""v3.3.1 regression: a user with a recorded join request must pass the gate
on the Retry re-check AND on the delivery-time re-check (two separate calls to
check_or_gate). Previously the Retry callback deleted the request before
delivery, so the 2nd gate re-blocked -> 'fsub_gate'."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
from app.services import fsub

class FakeBot:
    def __init__(self): self.sent = []
    async def get_chat_member(self, chat_id, user_id):
        raise Exception("Bad Request: user not found")   # private channel, requester
    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text); return SimpleNamespace(message_id=1)

def test_requester_passes_both_gate_calls():
    import pytest
    # channel list: one private (request) channel; request recorded
    async def _list(): return [{"chat_id": -100777, "link": "https://t.me/+AbC", "title": "Priv"}]
    async def _has(cid, uid): return True
    orig_list, orig_has = fsub.list_fsub, fsub.repo.has_fsub_request
    fsub.list_fsub = _list
    fsub.repo.has_fsub_request = _has
    try:
        bot = FakeBot()
        # gate call #1 (Retry callback re-check) and #2 (deliver_to_user re-check)
        assert asyncio.run(fsub.unjoined_channels(bot, 42)) == []
        assert asyncio.run(fsub.unjoined_channels(bot, 42)) == []
        assert asyncio.run(fsub.check_or_gate(bot, 42, "code123")) is True
        assert bot.sent == []  # gate never fired
    finally:
        fsub.list_fsub, fsub.repo.has_fsub_request = orig_list, orig_has

def test_requester_passes_on_participant_phrasing():
    async def _list(): return [{"chat_id": -100777, "link": "https://t.me/+AbC", "title": "Priv"}]
    async def _has(cid, uid): return True
    orig_list, orig_has = fsub.list_fsub, fsub.repo.has_fsub_request
    fsub.list_fsub = _list; fsub.repo.has_fsub_request = _has
    class B2(FakeBot):
        async def get_chat_member(self, chat_id, user_id):
            raise Exception("Bad Request: user not a participant")
    try:
        assert asyncio.run(fsub.unjoined_channels(B2(), 42)) == []
    finally:
        fsub.list_fsub, fsub.repo.has_fsub_request = orig_list, orig_has


# v4.2: the 15-minute membership cache must not leak state between tests.
import pytest as _pytest
from app.services import fsub as _fsub_mod


@_pytest.fixture(autouse=True)
def _clear_fsub_member_cache():
    _fsub_mod._member_cache.clear()
