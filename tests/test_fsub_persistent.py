"""v3.3.2 regression: a recorded join request must satisfy the gate REPEATEDLY
(post #10, #11, #12...) — delivery must NOT delete the request."""
import sys, asyncio
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import inspect
from app.handlers import fsub_cmds
from app.services import fsub


class FakeBot:
    async def get_chat_member(self, chat_id, user_id):
        raise Exception("Bad Request: user not found")
    async def send_message(self, chat_id, text, **kw):
        return SimpleNamespace(message_id=1)


def test_no_purge_after_delivery_in_source():
    """The retry callback must not call remove_fsub_requests_for_user."""
    src = inspect.getsource(fsub_cmds.on_fsub_retry)
    assert "remove_fsub_requests_for_user" not in src, \
        "retry callback still purges recorded requests after delivery"


def test_request_passes_repeatedly():
    """Same requester, same channel, several gate checks in a row — all pass."""
    async def _list():
        return [{"chat_id": -1004399640463, "link": "https://t.me/+tTIVbr-7EwdhNGY1", "title": "F"}]
    async def _has(cid, uid):
        return True
    orig_list, orig_has = fsub.list_fsub, fsub.repo.has_fsub_request
    fsub.list_fsub, fsub.repo.has_fsub_request = _list, _has
    try:
        bot = FakeBot()
        for _ in range(5):  # post #10, #11, #12... — must pass every time
            assert asyncio.run(fsub.unjoined_channels(bot, 42)) == []
            assert asyncio.run(fsub.check_or_gate(bot, 42, "c")) is True
    finally:
        fsub.list_fsub, fsub.repo.has_fsub_request = orig_list, orig_has


# v4.2: the 15-minute membership cache must not leak state between tests.
import pytest as _pytest
from app.services import fsub as _fsub_mod


@_pytest.fixture(autouse=True)
def _clear_fsub_member_cache():
    _fsub_mod._member_cache.clear()
