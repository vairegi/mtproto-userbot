"""Bot 0 v13.15 regressions. Offline: real routes/handlers, synthetic Mongo.
Run separately from legacy script suites: python -m pytest -q tests/test_v13_15_bot0.py
Test dependencies only: pytest, mongomock (not required in production).
"""
import asyncio
import hashlib
import hmac
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock
from urllib.parse import urlencode

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'miniapp/backend'))
# Synthetic values, never load production credentials or start a Telegram poller.
os.environ.update(BOT_TOKEN='123456:offline_test_token', ADMIN_USER_ID='1',
    MONGO_URI='mongodb://127.0.0.1:1', MONGO_DB_NAME='relaybot', BOT0_TURSO_OFF='1')
from app import auth, rootdb
from app.config import settings
from app.routes import gallery, queue, shortener, suggestions
from app.services import shortener as gate, similar_mongo as similar
from common import mongo2_client as m2
import db
import admin_bot


def signed_headers(uid=2):
    fields = {'auth_date': str(int(time.time())), 'user': json.dumps({'id': uid, 'first_name': 'Test'})}
    key = hmac.new(b'WebAppData', settings.bot_token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(key, '\n'.join(f'{k}={fields[k]}' for k in sorted(fields)).encode(), hashlib.sha256).hexdigest()
    return {'X-Telegram-Init-Data': urlencode(fields)}


@pytest.fixture
def env(monkeypatch):
    primary = mongomock.MongoClient().relaybot
    legacy = mongomock.MongoClient().turso_backup.turso_nhentai_cache
    conn = db.MongoHandle(primary)
    monkeypatch.setattr(db, 'connect', lambda: conn)
    monkeypatch.setattr(rootdb, 'load', lambda: NS(connect=lambda: conn))
    monkeypatch.setattr(gate, '_db', db)
    monkeypatch.setattr(admin_bot, 'settings', NS(admin_user_id=1))
    monkeypatch.setattr(m2, '_get_coll', lambda: legacy)
    monkeypatch.setenv('MONGO2_READS', '1')
    monkeypatch.delenv('SIMILAR_ENABLED', raising=False)
    monkeypatch.setenv('SIMILAR_SOURCE', 'mongo')
    primary.control_flags.insert_many([
        {'_id': 'shortener_enabled', 'value': '1'},
        {'_id': 'shortener_api_url', 'value': 'https://shortener.invalid/api?api=offline&url='},
    ])
    detail = {'id': '100', 'title': 'Synthetic gallery', 'cover': '/cover.png', 'pages': 1,
              'pages_meta': [{'n': 1, 'path': 'offline.webp', 'w': 200, 'h': 300}]}
    monkeypatch.setattr(gallery.scraper_bridge, 'gallery_detail', lambda gid: detail)
    monkeypatch.setattr(gallery.queue_bridge, 'gallery_status', lambda gid: {'known': False})
    monkeypatch.setattr(queue.db, 'get_public_mode', lambda: True)
    monkeypatch.setattr(queue.ratelimit, 'check_and_consume', lambda uid: {})
    monkeypatch.setattr(queue.ratelimit, 'usage_summary', lambda uid: {})
    enqueue = Mock(return_value={'id': 1})
    deliver = Mock(return_value={'ok': True, 'delivered': True, 'gallery_id': '100'})
    monkeypatch.setattr(queue.queue_bridge, 'enqueue', enqueue)
    monkeypatch.setattr(queue.queue_bridge, 'dedup_peek', lambda url: {'verdict': 'proceed'})
    monkeypatch.setattr(queue.dm_delivery, 'deliver_to_dm', deliver)
    monkeypatch.setattr(suggestions, 'attach_is_cached', lambda items: items)
    dm = Mock()
    monkeypatch.setattr(shortener, '_maybe_send_verify_dm', dm)
    app = FastAPI()
    for router in (gallery.router, queue.router, shortener.router, suggestions.router):
        app.include_router(router)
    client = TestClient(app)
    yield NS(primary=primary, legacy=legacy, conn=conn, client=client,
             headers=signed_headers(), enqueue=enqueue, deliver=deliver, dm=dm, detail=detail)
    client.close()


def unlock(e, uid=2):
    e.primary.shortener_unlocks.update_one({'_id': uid}, {'$set': {'unlocked_until': time.time()+3600}}, upsert=True)


def test_browse_without_verification_and_no_dm(env):
    r=env.client.get('/api/gallery/100', headers=env.headers)
    assert r.status_code == 200 and r.json()['title']=='Synthetic gallery'
    assert 'pages_meta' not in r.json()
    assert env.detail['pages_meta']  # redaction must not mutate shared cache
    assert env.client.get('/api/gallery/100/status', headers=env.headers).status_code == 200
    assert env.client.get('/api/shortener/status', headers=env.headers).json()['locked']
    env.dm.assert_not_called()


@pytest.mark.parametrize('method,path,body',[
    ('get','/api/gallery/100/read',None),
    ('post','/api/queue',{'url':'https://nhentai.net/g/100/'}),
    ('post','/api/queue/deliver/100',{}),
])
def test_server_rejects_locked_actions_before_side_effects(env,method,path,body):
    r=env.client.request(method,path,headers=env.headers,json=body)
    assert r.status_code==403 and r.json()['detail']['code']=='shortener_required'
    env.enqueue.assert_not_called(); env.deliver.assert_not_called()


def test_status_prompt_only_on_explicit_action(env):
    r=env.client.get('/api/shortener/status?prompt=true',headers=env.headers)
    assert r.status_code==200 and r.json()['locked']
    env.dm.assert_called_once_with(2)
    env.client.get('/api/shortener/status',headers=env.headers)
    assert env.dm.call_count==1
    unlock(env)
    assert not env.client.get('/api/shortener/status?prompt=true',headers=env.headers).json()['locked']
    assert env.dm.call_count==1


@pytest.mark.parametrize('uid,mode',[(2,'unlock'),(1,'admin'),(2,'disabled'),(2,'unconfigured'),(2,'db-outage')])
def test_actions_permitted_for_valid_unlock_admin_disabled_or_failopen(env,monkeypatch,uid,mode):
    if mode=='unlock': unlock(env)
    if mode=='disabled': env.primary.control_flags.update_one({'_id':'shortener_enabled'},{'$set':{'value':'0'}})
    if mode=='unconfigured': env.primary.control_flags.update_one({'_id':'shortener_api_url'},{'$set':{'value':''}})
    if mode=='db-outage': monkeypatch.setattr(gate,'_unlocks',Mock(side_effect=RuntimeError('offline')))
    headers=signed_headers(uid)
    assert env.client.get('/api/gallery/100/read',headers=headers).json()['pages_meta']
    assert env.client.post('/api/queue',headers=headers,json={'url':'https://nhentai.net/g/100/'}).status_code==200
    assert env.client.post('/api/queue/deliver/100',headers=headers,json={}).json()['delivered']
    env.enqueue.assert_called_once(); env.deliver.assert_called_once()


def test_expired_unlock_and_user_isolation(env):
    unlock(env)
    assert gate.is_verified(2,1)
    assert not gate.is_verified(3,1)
    env.primary.shortener_unlocks.update_one({'_id':2},{'$set':{'unlocked_until':time.time()-1}})
    assert env.client.get('/api/gallery/100/read',headers=env.headers).status_code==403


def test_real_token_roundtrip_unlock_expiry_reset(env):
    token=gate.mint_token(2)
    assert not gate.is_verified(2,1)
    assert gate.verify_token(token)==2
    assert gate.is_verified(2,1) and not gate.is_verified(3,1)
    visits=env.primary.shortener_unlocks.find_one({'_id':2})['visits_total']
    assert gate.verify_token(token)==2
    assert env.primary.shortener_unlocks.find_one({'_id':2})['visits_total']==visits
    assert gate.verify_token('not-a-token') is None


@pytest.mark.parametrize('path',['/api/gallery/100','/api/gallery/100/read','/api/shortener/status','/api/gallery/100/suggestions'])
def test_real_auth_rejects_missing_and_forged_signatures(env,path):
    assert env.client.get(path).status_code==401
    assert env.client.get(path,headers={'X-Telegram-Init-Data':'user=%7B%22id%22%3A1%7D&hash=forged'}).status_code==401


def payload(gid, artist='same', tag='shared'):
    return {'id':gid,'title':f'Test {gid}','cover':'/cover.png','pages':10,
            'tag_groups':{'artist':[artist],'tag':[tag]}}


def populate(coll, count=10):
    for gid in range(100,100+count):
        coll.insert_one({'_id':f'gallery:{gid}','payload':json.dumps(payload(gid)), 'expires_at':0})
    coll.insert_one({'_id':'gallery:999','payload':payload(999,'different','different')})
    coll.insert_one({'_id':'gallery:998','payload':'broken-json'})


def test_recommendations_use_primary_without_mongo2_target(env):
    populate(env.primary.nhentai_cache)
    r=env.client.get('/api/gallery/100/suggestions?limit=8',headers=env.headers)
    assert r.status_code==200 and len(r.json()['items'])==8
    ids=[str(x['id']) for x in r.json()['items']]
    assert '100' not in ids and '999' not in ids
    assert env.legacy.count_documents({})==0


def test_legacy_fallback_and_mongo2_reads_switch(env,monkeypatch):
    populate(env.legacy)
    assert similar.similar_galleries('100',6)
    monkeypatch.setenv('MONGO2_READS','0')
    assert similar.similar_galleries('100',6)==[]


def test_primary_failure_still_uses_legacy(env,monkeypatch):
    populate(env.legacy)
    monkeypatch.setattr(rootdb,'load',Mock(side_effect=RuntimeError('offline')))
    assert len(similar.similar_galleries('100',6))==6


def test_old_or_empty_cache_does_not_hide_new_results(env):
    populate(env.primary.nhentai_cache)
    for key in ('similar:100','similar:v13.15:100'):
        env.primary.nhentai_cache.insert_one({'_id':key,'items':[],'cached_at':time.time()})
    assert len(similar.similar_galleries('100',8))==8


def test_cache_limit_increase_then_hit_no_scan(env,monkeypatch):
    populate(env.primary.nhentai_cache)
    assert len(similar.similar_galleries('100',2))==2
    assert len(similar.similar_galleries('100',8))==8
    monkeypatch.setattr(similar,'_scan_and_score',Mock(side_effect=AssertionError('warm cache scanned')))
    assert len(similar.similar_galleries('100',8))==8


def test_disabled_invalid_missing_and_zero_overlap(env,monkeypatch):
    assert similar.similar_galleries('not-an-id')==[]
    assert similar.similar_galleries('100')==[]
    populate(env.primary.nhentai_cache,1)
    assert similar.similar_galleries('100')==[]
    assert not env.primary.nhentai_cache.find_one({'_id':'similar:v13.15:100'})
    monkeypatch.setenv('SIMILAR_ENABLED','0')
    assert env.client.get('/api/gallery/100/suggestions',headers=env.headers).json()['items']==[]


def test_turso_off_never_calls_sql_even_if_source_is_turso(env,monkeypatch):
    sql=Mock(side_effect=AssertionError('Turso called while disabled'))
    monkeypatch.setattr(suggestions.scraper_bridge,'gallery_suggestions',sql)
    for source in ('mongo','turso'):
        monkeypatch.setenv('SIMILAR_SOURCE',source)
        assert env.client.get('/api/gallery/100/suggestions',headers=env.headers).status_code==200
    sql.assert_not_called()


def update_for(text,uid=1):
    replies=[]
    async def reply(s): replies.append(s)
    return NS(effective_user=NS(id=uid),effective_message=NS(text=text,reply_text=reply)),replies


def seed_queue(e):
    for n,status in enumerate(['pending','processing','completed','done','failed','cancelled'],1):
        e.primary.queue.insert_one({'_id':n,'url':f'https://nhentai.net/g/{100+n}/',
            'gallery_id':str(100+n),'status':status,'created_at':time.time(),'updated_at':time.time(),
            'submitted_by':2,'username':'test','url_hash':str(n)})
    e.primary.galleries.insert_one({'_id':'102','status':'PROCESSING','db_pdf_msg_id':50})


def command(e,text,uid=1):
    upd,replies=update_for(text,uid)
    asyncio.run(admin_bot.cmd_cleanrequest(upd,NS(args=text.split()[1:])))
    assert not any('Internal error' in s for s in replies),replies
    return replies


def test_cleanup_full_preview_confirm_counts_preserve_durable_data(env):
    seed_queue(env)
    before=list(env.primary.queue.find())
    assert 'nothing changed yet' in command(env,'/cleanrequest')[0]
    assert list(env.primary.queue.find())==before
    reply=command(env,'/cleanrequest confirm')[0]
    assert 'pending: 0' in reply and 'processing: 0' in reply
    assert env.primary.queue.count_documents({'status':'cancelled'})==3
    assert env.primary.queue.find_one({'_id':3})['status']=='completed'
    assert env.primary.galleries.find_one({'_id':'102'})['status']=='PROCESSING'
    assert 'nothing to do' in command(env,'/cleanrequest')[0]
    assert 'removed (all open requests):   0' in command(env,'/cleanrequest confirm')[0]


@pytest.mark.parametrize('text',['/cleanrequest unconfirmed','/cleanrequest confirm-no','/cleanrequest confirm extra'])
def test_cleanup_strict_confirmation_and_unauthorized(env,text):
    seed_queue(env)
    command(env,text)
    assert env.primary.queue.count_documents({'status':{'$in':['pending','processing']}})==2
    assert command(env,'/cleanrequest confirm',uid=3)==[]
    assert env.primary.queue.find_one({'_id':1})['status']=='pending'


def test_cleanup_completion_race_does_not_cancel_completed(env,monkeypatch):
    seed_queue(env)
    original=admin_bot._cleanrequest_scan
    def race(conn):
        rows=original(conn)
        conn.queue.update_one({'_id':2},{'$set':{'status':'completed'}})
        return rows
    monkeypatch.setattr(admin_bot,'_cleanrequest_scan',race)
    command(env,'/cleanrequest confirm')
    assert env.primary.queue.find_one({'_id':2})['status']=='completed'
    assert env.primary.queue.find_one({'_id':1})['status']=='cancelled'


def test_bot2_does_not_resurrect_cancelled_ledger(env):
    seed_queue(env)
    command(env,'/cleanrequest confirm')
    spec=importlib.util.spec_from_file_location('bot2_queue_contract',ROOT/'Bot2Fetcher/app/mongo_state.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    gs=mod.Galleries(env.primary,900)
    gs.mark_queue_status('102','completed')
    assert env.primary.queue.find_one({'_id':2})['status']=='cancelled'


def test_actual_main_mounts_routes_startup_health_and_static(env,monkeypatch):
    # Execute actual startup callbacks, replacing only external schema/loops.
    from app.services import deletion_scheduler, delivery_watcher
    # This revision lacks turso_schema: real startup catches the missing
    # optional import non-fatally. Do not invent the deleted module.
    deletion=Mock(); watcher=Mock()
    monkeypatch.setattr(deletion_scheduler,'start_background_loop',deletion)
    monkeypatch.setattr(delivery_watcher,'start_background_loop',watcher)
    spec=importlib.util.spec_from_file_location('actual_bot0_main',ROOT/'miniapp/backend/main.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    with TestClient(mod.app) as client:
        assert client.get('/healthz').json()['ok']
        assert client.get('/').status_code==200
        asset=client.get('/static/js/plugins/shortener-gate.js')
        assert asset.status_code==200 and 'no-store' in asset.headers['cache-control']
        assert client.get('/api/gallery/100/read',headers=env.headers).status_code==403
        assert client.get('/api/shortener/status',headers=env.headers).status_code==200
        assert client.get('/api/gallery/100/suggestions?limit=13',headers=env.headers).status_code==422
    deletion.assert_called_once();watcher.assert_called_once()


def test_primary_hit_never_connects_legacy(env,monkeypatch):
    populate(env.primary.nhentai_cache)
    legacy=Mock(side_effect=AssertionError('primary hit contacted legacy'))
    monkeypatch.setattr(m2,'_get_coll',legacy)
    assert len(similar.similar_galleries('100',8))==8
    legacy.assert_not_called()


def test_short_result_cache_is_reused_for_same_limit(env,monkeypatch):
    populate(env.primary.nhentai_cache,3)
    assert len(similar.similar_galleries('100',8))==2
    scan=Mock(side_effect=AssertionError('repeat request rescanned'))
    monkeypatch.setattr(similar,'_scan_and_score',scan)
    assert len(similar.similar_galleries('100',8))==2
    scan.assert_not_called()


def test_scan_failure_releases_admission_slot(env,monkeypatch):
    populate(env.primary.nhentai_cache)
    monkeypatch.setattr(similar,'_scan_and_score',Mock(side_effect=RuntimeError('offline cursor error')))
    assert similar.similar_galleries('100',8)==[]
    assert similar._SCAN_SLOTS.acquire(blocking=False)
    similar._SCAN_SLOTS.release()


def test_artist_overlap_outranks_single_content_tag(env):
    coll=env.primary.nhentai_cache
    for gid,artist,tag in [(100,'a','shared'),(101,'a','other'),(102,'b','shared')]:
        coll.insert_one({'_id':f'gallery:{gid}','payload':payload(gid,artist,tag)})
    assert [x['id'] for x in similar.similar_galleries('100',6)]==[101,102]


def test_private_mode_still_rejects_download(env,monkeypatch):
    unlock(env)
    monkeypatch.setattr(queue.db,'get_public_mode',lambda:False)
    for path,body in [('/api/queue',{'url':'https://nhentai.net/g/100/'}),('/api/queue/deliver/100',{})]:
        assert env.client.post(path,headers=env.headers,json=body).status_code==403
    env.enqueue.assert_not_called();env.deliver.assert_not_called()


def test_live_dm_helper_throttles_repeated_action_prompts(env,monkeypatch):
    # Restore the real helper (fixture replaces it to isolate API tests).
    real=importlib.reload(shortener)
    monkeypatch.setattr(real,'_bot_token',lambda:'123456:offline')
    monkeypatch.setattr(real,'_bot_username',lambda:'OfflineBot')
    monkeypatch.setattr(real.svc,'shorten',lambda url:url)
    send=Mock(return_value=NS(status_code=200))
    monkeypatch.setattr(real.httpx,'post',send)
    real._last_dm.clear()
    real._maybe_send_verify_dm(2)
    real._maybe_send_verify_dm(2)
    assert send.call_count==1
    sent=send.call_args.kwargs['json']
    assert sent['chat_id']==2 and 'verify_' in sent['reply_markup']['inline_keyboard'][0][0]['url']
