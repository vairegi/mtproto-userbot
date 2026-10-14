"""v13.11 offline integration: actual handlers/lifecycle; mocked Telegram & Mongo.
Run: python3 Bot2Fetcher/tests/test_workers_v13_11.py
Dependencies for this test only: mongomock (not required in production).
"""
from __future__ import annotations
import asyncio
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import mongomock
from telethon.crypto import AuthKey
from telethon.sessions import StringSession
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters
from telegram.request import BaseRequest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'Bot2Fetcher'))
from app import workers, fetcher as ft, mongo_state as ms, admin_bot as ab, config, dashboard


def session(n):
    s = StringSession()
    s.set_dc(2, '149.154.167.51', 443)
    s.auth_key = AuthKey(bytes([n]) * 256)
    return s.save()


class Client:
    revoked = set()
    fail_resolve = set()
    duplicates = {}
    made = []
    def __init__(self, sess, api_id, api_hash):
        self.session = sess
        self.n = sess.auth_key.key[0]
        self.api_id = api_id
        self.api_hash = api_hash
        self.sent = []
        self.forwards = []
        self.disconnected = False
        Client.made.append(self)
    async def connect(self):
        pass
    async def disconnect(self):
        self.disconnected = True
    async def is_user_authorized(self):
        return self.n not in self.revoked
    async def get_me(self):
        return NS(id=self.duplicates.get(self.n, self.n), username=f'worker{self.n}')
    async def iter_dialogs(self, limit):
        if False:
            yield None
    async def get_input_entity(self, key):
        if self.n in self.fail_resolve:
            raise ValueError('not a channel member')
        return key
    async def send_message(self, entity, text):
        self.sent.append((entity, text))
        return NS(id=100, out=True)
    async def get_messages(self, entity, limit=15):
        return [NS(id=101, out=False, document=object(), raw_text='')]
    async def forward_messages(self, channel, ids, source, drop_author):
        self.forwards.append((channel, ids, source, drop_author))
        return [NS(id=200+i) for i in range(len(ids))] if isinstance(ids, list) else NS(id=300)


class Api:
    """Mock getMe, never polls the live bot."""
    def __init__(self, *a, **kw):
        pass
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def get(self, url):
        return NS(json=lambda: {'ok': True, 'result': {'id': 999999, 'username': 'MyWorkerAdminBot'}})


def settings():
    return NS(api_id=0, api_hash='', bot2_username='@MyWorkerAdminBot',
              fallback_username='', db_channel_id='-100100',
              fetch_gap_min=0.01, fetch_gap_max=0.01, retry_cooldown_s=1800,
              rescan_sleep_s=1, scan_max_galleries=2000, fallback_timeout_s=1,
              both_fail_park_s=43200, log_bot_token='', log_channel_id='')


def make_fetcher(db):
    f = ft.Fetcher(settings(), ms.Galleries(db, 900), NS(), ft.Stats())
    f.workers_db = db
    f.peer_cache = ms.PeerCache(db)
    return f


async def lifecycle():
    db = mongomock.MongoClient().test
    f = make_fetcher(db)
    with patch.object(ft, 'TelegramClient', Client), patch.object(ft.httpx, 'AsyncClient', Api):
        await f.start()
        assert not f.clients
        assert f.downloader_username == 'Gallery_DLBot'
        print('PASS startup: zero workers standby; management-bot destination rejected')
        workers.set_creds(db, '12345', 'a'*32)
        for i in range(1, 5):
            workers.add_worker(db, session(i), '12345', 'a'*32)
        # Preseed legacy cached wrong peer. New cache namespace must ignore it.
        for i in range(1, 5):
            old = Client(StringSession(session(i)), 12345, 'a'*32)
            f.peer_cache.put(i, 'bot2_user', 'MyWorkerAdminBot', ms.PeerCache.fp(old))
        await f.reconcile_workers()
        await asyncio.sleep(0)
        assert all(f.worker_status()[i]['running'] for i in range(1, 5))
        assert all(v == 'Gallery_DLBot' for v in f._bot2_per_slot.values())
        ft.POLL_EVERY_S = 0.005
        # Four real _do_job pipelines with only external I/O/metadata mocked.
        f.turso = NS(get_gallery_row=AsyncMock(return_value={}), put_state=AsyncMock())
        f._fetch_cover_bytes = AsyncMock(return_value=(b'cover', '.jpg'))
        f._post_cover = AsyncMock(side_effect=[11, 21, 31, 41])
        f._mirror_to_backup = AsyncMock()
        meta = lambda gid, row: {'id': gid, 'title': 'test', 'pages': 10, 'cover': 'test'}
        with patch.object(ft.meta_mod, 'meta_from_cache', side_effect=meta), patch.object(ft.meta_mod, 'caption_for', return_value='test'):
            results = await asyncio.gather(*(f._do_job(i, f._slot_clients[i], str(1000+i)) for i in range(1, 5)))
        assert all(results) and f.stats.completed == 4
        assert all(db.galleries.find_one({'_id': str(1000+i)})['status'] == 'COMPLETED' for i in range(1, 5))
        assert all(f.worker_status()[i]['completed'] == 1 for i in range(1, 5))
        assert all(c.sent[0][0] == 'Gallery_DLBot' for c in f._slot_clients.values())
        print('PASS four concurrent PDF job pipelines: each worker received and posted its own PDF')
        # A task that exits is detected and recreated.
        old = f._slot_clients[3]
        f._slot_tasks[3].cancel()
        await asyncio.gather(f._slot_tasks[3], return_exceptions=True)
        await f.reconcile_workers()
        assert old.disconnected and f._slot_clients[3] is not old
        print('PASS dead worker task reconnect')
        # Real Mongo _id renumber semantics (not mutable test dicts).
        assert workers.remove_workers(db, [1, 3]) == [1, 3]
        assert [w['_id'] for w in workers.list_workers(db)] == ['slot:1', 'slot:2']
        await f.reconcile_workers()
        assert [f._slot_clients[i].n for i in (1, 2)] == [2, 4]
        assert workers.set_creds(db, '67890', 'b'*32)
        await f.reconcile_workers()
        assert all(c.api_id == 67890 for c in f.clients)
        assert all(w['api_id'] == '67890' for w in workers.list_workers(db))
        workers.clear_workers(db)
        await f.reconcile_workers()
        assert not f.clients
        print('PASS multi-removal/renumber, credential reconnect, remove-all standby')
        # A broken slot must not prevent a healthy later slot from starting.
        for i in range(1, 5):
            workers.add_worker(db, session(i), '67890', 'b'*32)
        Client.revoked = {2}
        Client.fail_resolve = {3}
        # Force cold lookup; warm peers intentionally bypass get_input_entity.
        f.peer_cache.coll.delete_many({'_id': {'$regex': '^slot3:'}})
        await f.reconcile_workers()
        assert sorted(f._slot_clients) == [1, 4]
        assert f.worker_status()[2]['state'] == 'error'
        assert f.worker_status()[3]['state'] == 'error'
        assert Client.made[-3].disconnected and Client.made[-2].disconnected
        print('PASS revoked and peer-failed slots isolated; later healthy slot starts')
        Client.revoked = set()
        Client.fail_resolve = set()
        await f.reconcile_workers()
        assert len(f.clients) == 4
        # Different session strings for SAME account are unsafe in parallel.
        workers.add_worker(db, session(5), '67890', 'b'*32)
        Client.duplicates = {5: 1}
        await f.reconcile_workers()
        assert len(f.clients) == 4 and 'duplicate' in f.worker_status()[5]['error']
        Client.duplicates = {}
        print('PASS failed peers retry; duplicate-account session refused')
        await f.stop()
        await asyncio.gather(*f._slot_tasks.values(), return_exceptions=True)


class Request(BaseRequest):
    """PTB transport for real Application.initialize/process_update dispatch."""
    def __init__(self):
        self.replies = []
    @property
    def read_timeout(self):
        return 10
    async def initialize(self):
        pass
    async def shutdown(self):
        pass
    async def do_request(self, url, method, request_data=None, **kw):
        name = url.rsplit('/',1)[-1]
        if name == 'getMe':
            result = {'id':999999, 'is_bot':True, 'first_name':'test', 'username':'MyWorkerAdminBot'}
        elif name == 'sendMessage':
            params = request_data.parameters
            self.replies.append(params['text'])
            result = {'message_id':len(self.replies), 'date':1,
                      'chat':{'id':int(params['chat_id']), 'type':'private'}, 'text':params['text']}
        else:
            result = True
        return 200, json.dumps({'ok':True, 'result':result}).encode()


async def commands():
    db = mongomock.MongoClient().test
    rt = {1:{'state':'idle', 'running':True, 'completed':2, 'account_id':1,
             'last_result':'PDF received', 'last_downloader':'Gallery_DLBot'}}
    adm = ab.WorkerAdminBot(db, status_provider=lambda:rt)
    transport = Request()
    app = ApplicationBuilder().token('999999:TEST').request(transport).get_updates_request(Request()).build()
    for name, handler in [('start',adm._cmd_start), ('addworker',adm._cmd_addworker),
                          ('listworker',adm._cmd_listworker), ('removeworker',adm._cmd_removeworker),
                          ('changeapiandhash',adm._cmd_changeapi), ('cancel',adm._cmd_cancel),
                          ('checkram',adm._cmd_checkram)]:
        app.add_handler(CommandHandler(name,handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,adm._on_text))
    await app.initialize()
    async def send(text, uid=123):
        message = {'message_id':1, 'date':1, 'chat':{'id':uid,'type':'private'},
                   'from':{'id':uid,'is_bot':False,'first_name':'admin'},'text':text}
        if text.startswith('/'):
            message['entities']=[{'type':'bot_command','offset':0,'length':len(text.split()[0])}]
        u = Update.de_json({'update_id':1,'message':message}, app.bot)
        count = len(transport.replies)
        await app.process_update(u)
        return transport.replies[-1] if len(transport.replies)>count else None
    assert '/addworker' in await send('/start')
    assert 'Step 1/3' in await send('/addworker')
    assert 'must be a number' in await send('bad')
    assert 'Step 2/3' in await send('12345')
    assert 'looks wrong' in await send('bad')
    assert 'Step 3/3' in await send('a'*32)
    assert "doesn't look" in await send('invalid'*10)
    assert 'Connection is pending' in await send(session(1))
    assert 'saved API creds' in await send('/addworker')
    assert 'already stored' in await send(session(1))
    assert 'saved' in await send(session(2))
    assert 'task=running' in await send('/listworker')
    assert 'PDF received' in transport.replies[-1]
    assert session(1) not in transport.replies[-1]
    assert await send('/listworker',456) is None
    assert 'Step 1/2' in await send('/changeapiandhash')
    await send('67890')
    assert 'updated' in await send('b'*32)
    assert workers.get_creds(db) == ('67890','b'*32)
    assert 'Bad slot' in await send('/removeworker bad')
    assert 'renumbered' in await send('/removeworker 1')
    assert workers.list_workers(db)[0]['session'] == session(2)
    assert 'ALL' in await send('/removeworker all')
    assert 'No workers' in await send('/listworker')
    assert 'RAM:' in await send('/checkram')
    # Data persists across admin listener instances/restart.
    other = ab.WorkerAdminBot(db)
    assert workers.get_creds(other._db) == ('67890','b'*32)
    await send('/addworker')
    assert 'Cancelled' in await send('/cancel')
    await app.shutdown()
    print('PASS PTB startup + /start reply + all wizard/list/remove/change/cancel/checkram command dispatch (mock Telegram)')


async def auxiliary():
    # Shared token dashboard must never consume PTB worker updates.
    d = dashboard.Dashboard(NS(log_bot_token='999999:TEST',log_channel_id='-1001'), NS(),ft.Stats())
    d.bot=NS(_call=AsyncMock())
    await d._poll_commands()
    d.bot._call.assert_not_called()
    print('PASS shared-token dashboard: no competing getUpdates consumer')
    class UnauthorizedApi(Api):
        calls = 0
        async def post(self, url, json):
            UnauthorizedApi.calls += 1
            return NS(json=lambda: {'ok':False,'error_code':401,'description':'Unauthorized'})
    lb = dashboard.LogBot('999999:TEST','-1001')
    with patch.object(dashboard.httpx,'AsyncClient',UnauthorizedApi):
        assert await lb._call('getMe',{}) is None
        assert await lb._call('editMessageText',{}) is None
        assert UnauthorizedApi.calls == 1
    print('PASS invalid dashboard token: stop API retry storm after first 401')
    # Peer ID guard remains effective even if admin getMe is unavailable.
    class IdentityClient(Client):
        async def get_input_entity(self,key):
            return NS(user_id=999999) if key=='MyWorkerAdminBot' else key
    f_guard = make_fetcher(mongomock.MongoClient().test)
    await f_guard._warm_and_resolve(1,IdentityClient(StringSession(session(1)),12345,'a'*32))
    assert f_guard.downloader_username=='Gallery_DLBot'
    assert f_guard._bot2_per_slot[1]=='Gallery_DLBot'
    print('PASS peer-ID self-bot guard without getMe; wrong destination replaced')
    # Backup is one server-side forward of the ALREADY-posted pair.
    db = mongomock.MongoClient().test
    f = make_fetcher(db)
    client = Client(StringSession(session(1)),12345,'a'*32)
    mc = mongomock.MongoClient()
    mc.test.galleries.insert_one({'_id':'999','status':'COMPLETED', 'cover_msg_id':11,'pdf_msg_id':12})
    with patch('pymongo.MongoClient', return_value=mc), patch.dict(os.environ, {'MONGO_URI':'mongodb://test','MONGO_DB_NAME':'test','BACKUP_DB_CHANNEL_ID':'-100200'}):
        await f._mirror_to_backup(client, '-100100', '999', 11, 12)
    assert client.forwards == [(-100200,[11,12],'-100100',True)]
    assert mc.test.galleries.find_one({'_id':'999'})['backup_pdf_msg_id']==201
    assert not client.sent
    print('PASS backup: primary pair forwarded once; no second downloader request')
    with patch.dict(os.environ, {'MONGO_URI':'mongodb://test', 'TURSO_DATABASE_URL':'https://test',
                              'TURSO_AUTH_TOKEN':'TEST','DB_CHANNEL_ID':'-100100','PORT':'18765',
                              'BOT2_USERNAME':'@WrongAdmin', 'PDF_DOWNLOADER_USERNAME':'@Gallery_DLBot'}):
        s = config.load()
        assert s.port==18765 and s.bot2_username=='Gallery_DLBot' and not s.sessions
    print('PASS config: downloader env precedence, @ normalization, PORT binding value, no env sessions required')


async def entrypoint():
    """Actual main module _main startup + /healthz ASGI check; external services stubbed."""
    import httpx
    db = mongomock.MongoClient().test
    with patch.dict(os.environ, {'MONGO_URI':'mongodb://test', 'TURSO_DATABASE_URL':'https://test',
                               'TURSO_AUTH_TOKEN':'TEST', 'DB_CHANNEL_ID':'-100100',
                               'PDF_DOWNLOADER_USERNAME':'Gallery_DLBot','PORT':'18765'}), patch.object(ms,'connect', return_value=db):
        spec = importlib.util.spec_from_file_location('bot2_entrypoint_test', ROOT/'Bot2Fetcher/main.py')
        main = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(main)
    with patch.object(main._admin_bot,'start', return_value=True), patch.object(main.turso,'ensure_schema',new=AsyncMock()), patch.object(main.dashboard,'run',new=AsyncMock()), patch.object(ft.httpx,'AsyncClient', Api):
        task=asyncio.create_task(main._main())
        await asyncio.sleep(0.04)
        assert not task.done() and not main.fetcher.clients
        main.fetcher._stop.set()
        await asyncio.wait_for(task,2)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://sandbox') as c:
        response=await c.get('/healthz')
        assert response.status_code==200 and response.json()=={'ok':True,'slots':0}
    server = main.uvicorn.Server(main.uvicorn.Config(main.app,host='0.0.0.0',port=main.settings.port,log_level='error'))
    server_task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        async with httpx.AsyncClient() as c:
            response = await c.get('http://127.0.0.1:18765/healthz')
            assert response.status_code==200 and response.json()['slots']==0
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task,3)
    with patch.object(main.uvicorn,'run') as run:
        main._run_api()
        assert run.call_args.kwargs['host']=='0.0.0.0' and run.call_args.kwargs['port']==18765
    record = logging.LogRecord('test',logging.INFO,'',1,'https://api.telegram.org/bot999999:FAKESECRET/getMe',(),None)
    main._RedactBotToken().filter(record)
    assert 'FAKESECRET' not in record.getMessage()
    print('PASS actual _main startup + live sandbox HTTP socket /healthz 200 + uvicorn 0.0.0.0:$PORT + token redaction (mock external services)')


async def main():
    with patch.dict(os.environ, {'BOT2_TOKEN':'999999:TEST','ADMIN_USER_ID':'123'}):
        await lifecycle()
        await commands()
        await auxiliary()
        await entrypoint()
    print('ALL v13.11 offline integration tests PASS (no live Telegram polling or Mongo writes)')


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
