"""v13.13 offline tests: HEAD/GET ping endpoints + /pauseworker /resumeworker.
Run from repo root: python3 Bot2Fetcher/tests/test_v13_13_ping_pause.py
"""
from __future__ import annotations
import asyncio, importlib.util, itertools, json, os, sys
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import mongomock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Bot2Fetcher'))
from app import workers


def test_pause_store():
    db = mongomock.MongoClient().test
    assert workers.get_paused(db) == set()
    workers.add_worker(db, 's' * 60, '1', 'a' * 32)
    workers.add_worker(db, 't' * 60, '1', 'a' * 32)
    assert workers.pause_workers(db, {1, 2}) == {1, 2}
    assert workers.get_paused(db) == {1, 2}
    assert workers.resume_workers(db, {1}) == {2}
    assert workers.resume_workers(db, None) == set()
    assert len(workers.list_workers(db)) == 2  # pause flags don't pollute slots
    print('PASS pause store roundtrip')


async def test_http_endpoints():
    import httpx
    db = mongomock.MongoClient().test
    with patch.dict(os.environ, {
            'MONGO_URI': 'mongodb://test', 'TURSO_DATABASE_URL': 'https://t',
            'TURSO_AUTH_TOKEN': 'T', 'DB_CHANNEL_ID': '-1001',
            'PDF_DOWNLOADER_USERNAME': 'Gallery_DLBot', 'PORT': '18001'}), \
         patch('app.mongo_state.connect', return_value=db):
        spec = importlib.util.spec_from_file_location(
            'bot2_main_v1313', ROOT / 'Bot2Fetcher/main.py')
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                 base_url='http://sandbox') as c:
        for path in ('/healthz', '/ping'):
            g = await c.get(path)
            assert g.status_code == 200 and g.json()['ok'] is True
            h = await c.head(path)
            assert h.status_code == 200, f'HEAD {path} must not be 405'
        assert (await c.head('/')).status_code == 200
        assert (await c.get('/')).status_code == 200
    print('PASS HTTP: GET+HEAD 200 on /healthz /ping / (UptimeRobot HEAD OK)')


async def test_pause_commands():
    from telegram import Update
    from telegram.ext import ApplicationBuilder, CommandHandler
    from telegram.request import BaseRequest
    from app import admin_bot as ab

    class Request(BaseRequest):
        def __init__(self): self.replies = []
        @property
        def read_timeout(self): return 10
        async def initialize(self): pass
        async def shutdown(self): pass
        async def do_request(self, url, method, request_data=None, **kw):
            name = url.rsplit('/', 1)[-1]
            if name == 'getMe':
                result = {'id': 999999, 'is_bot': True, 'first_name': 't',
                          'username': 'MyWorkerAdminBot'}
            elif name == 'sendMessage':
                self.replies.append(request_data.parameters['text'])
                result = {'message_id': 1, 'date': 1,
                          'chat': {'id': 123, 'type': 'private'},
                          'text': request_data.parameters['text']}
            else:
                result = True
            return 200, json.dumps({'ok': True, 'result': result}).encode()

    db = mongomock.MongoClient().test
    for i in range(1, 5):
        workers.add_worker(db, ('s%d' % i) * 40, '1', 'a' * 32)
    adm = ab.WorkerAdminBot(db)
    tr = Request()
    app = (ApplicationBuilder().token('999999:TEST').request(tr)
           .get_updates_request(Request()).build())
    app.add_handler(CommandHandler('pauseworker', adm._cmd_pauseworker))
    app.add_handler(CommandHandler('resumeworker', adm._cmd_resumeworker))
    await app.initialize()

    async def send(text, uid=123):
        cmd = text.split()[0]
        msg = {'message_id': 1, 'date': 1, 'chat': {'id': uid, 'type': 'private'},
               'from': {'id': uid, 'is_bot': False, 'first_name': 'a'},
               'text': text}
        if text.startswith('/'):
            msg['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(cmd)}]
        u = Update.de_json({'update_id': 1, 'message': msg}, app.bot)
        n = len(tr.replies)
        await app.process_update(u)
        return tr.replies[-1] if len(tr.replies) > n else None

    assert 'Usage' in await send('/pauseworker')
    assert 'No such worker' in await send('/pauseworker 9')
    r = await send('/pauseworker 1 3')
    assert 'Paused worker(s): [1, 3]' in r
    assert workers.get_paused(db) == {1, 3}
    await send('/pauseworker all')
    assert workers.get_paused(db) == {1, 2, 3, 4}
    assert 'Resumed' in await send('/resumeworker 2')
    assert workers.get_paused(db) == {1, 3, 4}
    await send('/resumeworker all')
    assert workers.get_paused(db) == set()
    assert await send('/pauseworker 1', uid=999) is None
    await app.shutdown()
    print('PASS commands: /pauseworker /resumeworker dispatch + validation + admin gate')


async def test_reconcile_pause():
    from app import fetcher as ft, mongo_state as ms
    from telethon.sessions import StringSession
    from telethon.crypto import AuthKey

    def mk(n):
        s = StringSession(); s.set_dc(2, '1.2.3.4', 443)
        s.auth_key = AuthKey(bytes([n]) * 256); return s.save()

    ids = itertools.count(1000)

    class C:
        def __init__(self, s_, a, h):
            self.disconnected = False
            self.myid = next(ids)
        async def connect(self): pass
        async def disconnect(self): self.disconnected = True
        async def is_user_authorized(self): return True
        async def get_me(self): return NS(id=self.myid, username='w')
        async def iter_dialogs(self, limit):
            if False: yield None
        async def get_input_entity(self, k): return k

    db = mongomock.MongoClient().test
    workers.set_creds(db, '1', 'a' * 32)
    for i in range(1, 5):
        workers.add_worker(db, mk(i), '1', 'a' * 32)
    s = NS(api_id=0, api_hash='', bot2_username='Gallery_DLBot',
           fallback_username='', db_channel_id='-1001', fetch_gap_min=0.01,
           fetch_gap_max=0.01, retry_cooldown_s=1800, rescan_sleep_s=1,
           scan_max_galleries=10, fallback_timeout_s=1, both_fail_park_s=43200,
           log_bot_token='', log_channel_id='')
    f = ft.Fetcher(s, ms.Galleries(db, 900), NS(), ft.Stats())
    f.workers_db = db
    f.peer_cache = ms.PeerCache(db)
    with patch.object(ft, 'TelegramClient', C):
        await f.reconcile_workers()
        await asyncio.sleep(0)
        assert len(f._slot_clients) == 4
        workers.pause_workers(db, {2, 4})
        await f.reconcile_workers()
        await asyncio.sleep(0)
        assert sorted(f._slot_clients) == [1, 3]
        st = f.worker_status()
        assert st[2]['paused'] is True and st[4]['paused'] is True
        assert st[1].get('paused') is False
        workers.resume_workers(db, None)
        await f.reconcile_workers()
        await asyncio.sleep(0)
        assert len(f._slot_clients) == 4
        await f.stop()
        await asyncio.gather(*f._slot_tasks.values(), return_exceptions=True)
    print('PASS reconcile: paused slots disconnect live, others keep working, resume reconnects')


async def _main():
    with patch.dict(os.environ, {'BOT2_TOKEN': '999999:TEST', 'ADMIN_USER_ID': '123'}):
        test_pause_store()
        await test_http_endpoints()
        await test_pause_commands()
        await test_reconcile_pause()
    print('ALL v13.13 ping+pause tests PASS (offline)')


if __name__ == '__main__':
    asyncio.run(_main())
