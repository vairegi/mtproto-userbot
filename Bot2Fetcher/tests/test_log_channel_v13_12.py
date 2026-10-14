"""v13.12 offline integration: Mongo-backed log channel (/addlog /removelog).

Covers: workers store roundtrip, PTB command dispatch (mock transport),
fetcher alert routing with BOT2_TOKEN fallback, dashboard hot-reload with
no env dependency. No live Telegram or Mongo traffic.

Run from repo root: python3 Bot2Fetcher/tests/test_log_channel_v13_12.py
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import mongomock
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from telegram.request import BaseRequest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Bot2Fetcher'))
from app import workers, fetcher as ft, dashboard as dm, admin_bot as ab


# --------------------------------------------------------------------------
# 1) workers store: set / get / clear
# --------------------------------------------------------------------------
def test_store():
    db = mongomock.MongoClient().test
    assert workers.get_log_channel(db) is None
    assert workers.set_log_channel(db, '-1003796521529')
    assert workers.get_log_channel(db) == '-1003796521529'
    # does not pollute the worker listing
    workers.add_worker(db, 's' * 60, '1', 'a' * 32)
    assert len(workers.list_workers(db)) == 1
    assert workers.clear_log_channel(db)
    assert workers.get_log_channel(db) is None
    # broken db -> fail-open
    class BadDB:
        def __getitem__(self, k):
            raise RuntimeError('down')
    assert workers.get_log_channel(BadDB()) is None
    print('PASS store: set/get/clear roundtrip + fail-open + no worker pollution')


# --------------------------------------------------------------------------
# 2) fetcher alert routing: Mongo chat wins, BOT2_TOKEN fallback token
# --------------------------------------------------------------------------
class PostStub:
    posts = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, json):
        PostStub.posts.append((url, json))
        return NS(status_code=200, text='ok')


def _mk_fetcher(db, log_bot_token='', log_channel_id=''):
    s = NS(log_bot_token=log_bot_token, log_channel_id=log_channel_id,
           fetch_gap_min=0.01, fetch_gap_max=0.01, retry_cooldown_s=1800,
           fallback_timeout_s=1, both_fail_park_s=43200, bot2_username='X',
           fallback_username='', db_channel_id='-1001', rescan_sleep_s=1,
           scan_max_galleries=10)
    f = ft.Fetcher(s, None, NS(), ft.Stats())
    f.workers_db = db
    return f


async def test_alert():
    db = mongomock.MongoClient().test
    f = _mk_fetcher(db)  # no env log config at all
    # nothing configured -> warning, no HTTP
    with patch.object(ft.httpx, 'AsyncClient', PostStub):
        PostStub.posts.clear()
        await f._alert_log_channel('test alert')
        assert PostStub.posts == []
    # Mongo channel + BOT2_TOKEN fallback token -> posts there
    workers.set_log_channel(db, '-100777')
    with patch.dict(os.environ, {'BOT2_TOKEN': '999999:TEST'}):
        with patch.object(ft.httpx, 'AsyncClient', PostStub):
            PostStub.posts.clear()
            await f._alert_log_channel('double failure')
            assert len(PostStub.posts) == 1
            url, payload = PostStub.posts[0]
            assert 'bot999999:TEST' in url
            assert payload['chat_id'] == -100777
            assert 'double failure' in payload['text']
    # Mongo wins over env LOG_CHANNEL_ID; dedicated token env wins over BOT2_TOKEN
    f2 = _mk_fetcher(db, log_bot_token='111:DEDICATED', log_channel_id='-100111')
    with patch.object(ft.httpx, 'AsyncClient', PostStub):
        PostStub.posts.clear()
        await f2._alert_log_channel('x')
        url, payload = PostStub.posts[0]
        assert 'bot111:DEDICATED' in url and payload['chat_id'] == -100777
    # env fallback when Mongo empty
    workers.clear_log_channel(db)
    f3 = _mk_fetcher(db, log_channel_id='-100222')
    with patch.object(ft.httpx, 'AsyncClient', PostStub):
        PostStub.posts.clear()
        await f3._alert_log_channel('y')
        assert PostStub.posts[0][1]['chat_id'] == -100222
    print('PASS alert: Mongo chat wins, env fallback, BOT2_TOKEN token fallback, '
          'silent-no-HTTP when unconfigured')


# --------------------------------------------------------------------------
# 3) dashboard: hot-reload without env (the user's exact bug)
# --------------------------------------------------------------------------
class StubLogBot:
    instances = []

    def __init__(self, token, chat):
        self.token = token
        self.chat_id = chat
        self.sent = []
        StubLogBot.instances.append(self)

    async def check(self):
        return True

    async def send_markdown(self, text):
        self.sent.append(text)
        return 555

    async def edit_markdown(self, mid, text):
        self.sent.append(text)
        return True

    async def _call(self, method, payload):
        return []


async def test_dashboard_hotreload():
    cfg = {'v': None}
    turso = NS(get_state=AsyncMock(return_value=None), put_state=AsyncMock())
    s = NS(log_bot_token='', log_channel_id='', log_summary_hours=99)
    d = dm.Dashboard(s, turso, ft.Stats())
    d._config_provider = lambda: cfg['v']
    fetcher = NS(_stop=asyncio.Event(), clients=[])
    with patch.dict(os.environ, {'BOT2_TOKEN': '999999:TEST'}):
        with patch.object(dm, 'LogBot', StubLogBot), \
             patch.object(dm, 'EDIT_EVERY_S', 0.01):
            task = asyncio.create_task(d.run(fetcher))
            await asyncio.sleep(0.08)
            # no config -> idle, no bot created, loop still alive
            assert not task.done()
            assert StubLogBot.instances == []
            # /addlog happens -> dashboard starts posting on its own
            cfg['v'] = '-100999'
            await asyncio.sleep(0.12)
            assert len(StubLogBot.instances) == 1
            bot = StubLogBot.instances[0]
            assert bot.token == '999999:TEST'      # BOT2_TOKEN fallback
            assert bot.chat_id in (-100999, "-100999")  # mongo value passed through as-is
            assert bot.sent, 'expected a status post after /addlog'
            # channel change -> new bot instance, fresh message (no id reuse)
            cfg['v'] = '-100888'
            await asyncio.sleep(0.12)
            assert len(StubLogBot.instances) == 2
            assert StubLogBot.instances[1].chat_id in (-100888, "-100888")
            # /removelog -> idles again
            cfg['v'] = None
            await asyncio.sleep(0.08)
            assert d.bot is None
            assert not task.done()
            fetcher._stop.set()
            await asyncio.wait_for(task, 2)
    print('PASS dashboard: no-env idle -> /addlog starts posting -> change -> '
          '/removelog idles, all without restart')


# --------------------------------------------------------------------------
# 4) admin commands via real PTB dispatch (mock transport)
# --------------------------------------------------------------------------
class Request(BaseRequest):
    def __init__(self):
        self.replies = []
        self.channel_posts = []
        self.fail_send = False

    @property
    def read_timeout(self):
        return 10

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kw):
        name = url.rsplit('/', 1)[-1]
        if name == 'getMe':
            result = {'id': 999999, 'is_bot': True, 'first_name': 't',
                      'username': 'MyWorkerAdminBot'}
        elif name == 'sendMessage':
            if self.fail_send:
                return 400, json.dumps({'ok': False, 'error_code': 400,
                                        'description': 'chat not found'}).encode()
            params = request_data.parameters
            cid = params['chat_id']
            if cid not in (123, '123'):
                self.channel_posts.append((cid, params['text']))
            result = {'message_id': 1, 'date': 1,
                      'chat': {'id': int(cid), 'type': 'channel'},
                      'text': params['text']}
        else:
            result = True
        return 200, json.dumps({'ok': True, 'result': result}).encode()


async def test_commands():
    db = mongomock.MongoClient().test
    adm = ab.WorkerAdminBot(db)
    transport = Request()
    app = (ApplicationBuilder().token('999999:TEST')
           .request(transport).get_updates_request(Request()).build())
    app.add_handler(CommandHandler('addlog', adm._cmd_addlog))
    app.add_handler(CommandHandler('removelog', adm._cmd_removelog))
    app.add_handler(CommandHandler('logstatus', adm._cmd_logstatus))
    app.add_handler(CommandHandler('start', adm._cmd_start))
    await app.initialize()

    async def send(text, uid=123):
        cmd = text.split()[0]
        message = {'message_id': 1, 'date': 1,
                   'chat': {'id': uid, 'type': 'private'},
                   'from': {'id': uid, 'is_bot': False, 'first_name': 'a'},
                   'text': text}
        if text.startswith('/'):
            message['entities'] = [{'type': 'bot_command', 'offset': 0,
                                    'length': len(cmd)}]
        u = Update.de_json({'update_id': 1, 'message': message}, app.bot)
        n = len(transport.replies)
        await app.process_update(u)
        return transport.replies[-1] if len(transport.replies) > n else None

    # capture admin replies too: bot.reply_text -> sendMessage to private chat
    orig = Request.do_request

    async def do_request(self, url, method, request_data=None, **kw):
        name = url.rsplit('/', 1)[-1]
        if name == 'sendMessage':
            params = request_data.parameters
            if params.get('chat_id') in (123, '123'):
                self.replies.append(params['text'])
        return await orig(self, url, method, request_data=request_data, **kw)

    Request.do_request = do_request

    assert '/addlog' in await send('/start')
    # usage
    r = await send('/addlog')
    assert 'Usage: /addlog' in r
    # bad id
    r = await send('/addlog notachannel')
    assert 'Bad channel id' in r
    # bot lacks admin rights -> loud failure, nothing saved
    transport.fail_send = True
    r = await send('/addlog -100555')
    assert 'Could not post' in r and 'ADMIN' in r
    assert workers.get_log_channel(db) is None
    # success: test post to channel + saved + reply
    transport.fail_send = False
    r = await send('/addlog -1003796521529')
    assert '✅ Log channel set to -1003796521529' in r
    assert workers.get_log_channel(db) == '-1003796521529'
    assert transport.channel_posts and \
        transport.channel_posts[0][0] == -1003796521529
    # logstatus reflects Mongo value + BOT2_TOKEN sender
    r = await send('/logstatus')
    assert 'Mongo (/addlog): -1003796521529' in r and 'BOT2_TOKEN' in r
    # non-admin ignored
    assert await send('/addlog -1001', uid=456) is None
    # removelog
    r = await send('/removelog')
    assert 'removed' in r and workers.get_log_channel(db) is None
    r = await send('/removelog')
    assert 'No Mongo log channel' in r
    await app.shutdown()
    print('PASS commands: /addlog usage/bad-id/no-admin-rights/success, '
          '/logstatus, /removelog, non-admin ignored')


async def main():
    with patch.dict(os.environ, {'BOT2_TOKEN': '999999:TEST',
                                 'ADMIN_USER_ID': '123'}):
        test_store()
        await test_alert()
        await test_dashboard_hotreload()
        await test_commands()
    print('ALL v13.12 log-channel tests PASS (offline, mocked Telegram/Mongo)')


if __name__ == '__main__':
    asyncio.run(main())
