"""Security and bandwidth regressions for the v13.16 follow-up.
Uses actual Bot 0 services/handlers with signed requests and synthetic Mongo.
Run alongside tests/test_v13_15_bot0.py. No external API calls or production writes.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import gzip
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import pytest
from bson import BSON

# Reuse the real API fixture, not copies of implementation code.
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_v13_15_bot0 import env, gate, shortener, similar, admin_bot, signed_headers, db, ROOT, update_for, payload, populate


def issued_token(e,monkeypatch,uid=2):
    monkeypatch.setattr(gate,'shorten',lambda deep:'https://short.invalid/code')
    gate.issue_link(uid,'OfflineBot')
    return e.primary.shortener_tokens.find_one({'uid':uid,'used':False,'issued':True})['_id']


@pytest.mark.parametrize('mode',['missing','bare-key','bad-scheme','provider-error','db-down'])
def test_failures_never_grant_read_or_download(env,monkeypatch,mode):
    if mode in ('missing','bare-key','bad-scheme'):
        value={'missing':'','bare-key':'not-a-url','bad-scheme':'ftp://invalid/api'}[mode]
        env.primary.control_flags.update_one({'_id':'shortener_api_url'},{'$set':{'value':value}})
    elif mode=='db-down': monkeypatch.setattr(gate,'_unlocks',Mock(side_effect=RuntimeError('offline')))
    else: monkeypatch.setattr(gate.httpx,'get',Mock(side_effect=RuntimeError('offline')))
    assert not gate.is_verified(2,1)
    for path,method in [('/api/gallery/100/read','get'),('/api/queue/deliver/100','post')]:
        assert env.client.request(method,path,headers=env.headers).status_code==403
    assert env.client.get('/api/gallery/100',headers=env.headers).status_code==200
    assert env.primary.shortener_unlocks.count_documents({})==0


def test_database_config_error_does_not_look_like_disabled(env,monkeypatch):
    monkeypatch.setattr(gate,'_flag',Mock(side_effect=RuntimeError('offline')))
    status=gate.status_for(2,1)
    assert status['locked'] and status['configuration_error']
    assert gate.is_verified(1,1)  # explicit configured admin remains available


@pytest.mark.parametrize('response',[
    {'status':'error','shortenedUrl':'https://short.invalid/code'},
    {'status':'success'},
    ['wrong-shape'],
    {'status':'success','url':'https://t.me/OfflineBot?start=verify_SECRET'},
    {'status':'success','url':'https://redirect.invalid/?url=https%3A%2F%2Ft.me%2FOfflineBot%3Fstart%3Dverify_SECRET'},
    {'status':'success','url':'javascript:alert(1)'},
])
def test_provider_rejection_or_direct_token_response_cannot_issue(env,monkeypatch,response):
    monkeypatch.setattr(gate.httpx,'get',lambda *a,**k:NS(raise_for_status=lambda:None,json=lambda:response))
    with pytest.raises(gate.ProviderUnavailable):
        gate.shorten('https://t.me/OfflineBot?start=verify_SECRET')
    assert not gate.is_verified(2,1)


def test_shortener_request_preserves_api_key_and_encodes_destination_once(env,monkeypatch):
    request=Mock(return_value=NS(raise_for_status=lambda:None,json=lambda:{'status':'success','shortenedUrl':'https://short.invalid/Ab1'}))
    monkeypatch.setattr(gate.httpx,'get',request)
    target='https://t.me/OfflineBot?start=verify_SECRET'
    assert gate.shorten(target)=='https://short.invalid/Ab1'
    query=parse_qs(urlsplit(request.call_args.args[0]).query)
    assert query=={'api':['offline'],'url':[target]}


def test_link_api_returns_only_provider_url_and_arms_private_token(env,monkeypatch):
    monkeypatch.setattr(shortener,'_bot_username',lambda:'OfflineBot')
    monkeypatch.setattr(gate,'shorten',lambda deep:'https://short.invalid/code')
    r=env.client.get('/api/shortener/link',headers=env.headers)
    assert r.status_code==200 and r.json()=={'ok':True,'url':'https://short.invalid/code'}
    token=env.primary.shortener_tokens.find_one({'uid':2})
    assert token['issued'] and token['gate_version']==2
    assert token['_id'] not in r.text and 'deep_link' not in r.text


@pytest.mark.parametrize('cause',['missing-scheme','http-error','no-bot','mongo-error'])
def test_link_api_failure_is_503_without_any_redeem_destination(env,monkeypatch,cause):
    monkeypatch.setattr(shortener,'_bot_username',lambda:'OfflineBot' if cause!='no-bot' else '')
    if cause=='missing-scheme':env.primary.control_flags.update_one({'_id':'shortener_api_url'},{'$set':{'value':'bare-api-key'}})
    if cause=='http-error':monkeypatch.setattr(gate.httpx,'get',Mock(side_effect=RuntimeError('secret URL not leaked')))
    if cause=='mongo-error':monkeypatch.setattr(gate,'mint_token',Mock(side_effect=RuntimeError('private connection')))
    r=env.client.get('/api/shortener/link',headers=env.headers)
    assert r.status_code==503
    assert not any(x in r.text for x in ('verify_','deep_link','bare-api-key','private connection','secret URL'))
    assert not gate.is_verified(2,1)
    assert not env.primary.shortener_tokens.find_one({'issued':True})


@pytest.mark.parametrize('kind',['legacy','unissued','expired','future','wrong-owner','already-used'])
def test_reject_nonredeemable_tokens_and_no_browser_unlock(env,monkeypatch,kind):
    token=gate.mint_token(2)
    updates={'issued':True}
    if kind=='legacy': updates['gate_version']=1
    if kind=='unissued': updates['issued']=False
    if kind=='expired': updates['created']=time.time()-gate.TOKEN_TTL-1
    if kind=='future': updates['created']=time.time()+60
    if kind=='wrong-owner': updates['uid']=3
    if kind=='already-used': updates['used']=True
    env.primary.shortener_tokens.update_one({'_id':token},{'$set':updates})
    assert gate.verify_token(token,2) is None
    assert gate.verify_token(token) is None
    assert env.client.get('/api/shortener/verify?token='+token).status_code==200
    assert env.primary.shortener_unlocks.count_documents({})==0


def test_valid_token_cannot_be_consumed_by_unauthenticated_browser(env,monkeypatch):
    token=issued_token(env,monkeypatch)
    r=env.client.get('/api/shortener/verify?token='+token)
    assert 'cannot grant access' in r.text
    assert not env.primary.shortener_tokens.find_one({'_id':token})['used']
    assert gate.verify_token(token,2)==2


def test_bot_start_uses_real_owner_bound_single_use_service(env,monkeypatch):
    token=issued_token(env,monkeypatch)
    upd,replies=update_for('/start verify_'+token,2)
    asyncio.run(admin_bot.cmd_start(upd,NS(args=['verify_'+token])))
    assert any('Verified' in x for x in replies)
    before=env.primary.shortener_unlocks.find_one({'_id':2})
    asyncio.run(admin_bot.cmd_start(upd,NS(args=['verify_'+token])))
    assert env.primary.shortener_unlocks.find_one({'_id':2})==before
    assert 'already used' in replies[-1]


def test_bare_start_back_and_new_download_do_not_grant_access(env,monkeypatch):
    upd,replies=update_for('/start',2)
    asyncio.run(admin_bot.cmd_start(upd,NS(args=[])))
    assert not replies and not gate.is_verified(2,1)
    assert env.client.get('/api/shortener/status?prompt=true',headers=env.headers).json()['locked']
    assert env.client.get('/api/shortener/status?prompt=true',headers=env.headers).json()['locked']
    assert env.client.post('/api/queue',headers=env.headers,json={'url':'https://nhentai.net/g/100/'}).status_code==403
    env.enqueue.assert_not_called()


def test_legacy_accidental_grant_requires_one_new_verification(env):
    env.primary.shortener_unlocks.insert_one({'_id':2,'unlocked_until':time.time()+86400})
    assert not gate.is_verified(2,1)
    assert not admin_bot._shortener_unlocked(2)


def test_same_token_concurrent_redemption_has_one_grant(env,monkeypatch):
    token=issued_token(env,monkeypatch)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lambda _:gate.verify_token(token,2),range(6)))
    assert results.count(2)==1
    assert env.primary.shortener_unlocks.find_one({'_id':2})['visits_total']==1


def test_unlock_write_failure_never_says_verified(env,monkeypatch):
    token=issued_token(env,monkeypatch)
    monkeypatch.setattr(env.primary.shortener_unlocks,'update_one',Mock(side_effect=RuntimeError('offline')))
    upd,replies=update_for('/start verify_'+token,2)
    asyncio.run(admin_bot.cmd_start(upd,NS(args=['verify_'+token])))
    assert not any('✅ Verified' in x for x in replies)
    assert not gate.is_verified(2,1)


def test_invalid_admin_api_not_saved_and_status_hides_api_key(env,monkeypatch):
    monkeypatch.setattr(admin_bot,'_shortener_is_admin',lambda u:True)
    upd,replies=update_for('/shortenerapi bare-key',1)
    asyncio.run(admin_bot.cmd_shortenerapi(upd,NS(args=['bare-key'])))
    assert 'Nothing was saved' in replies[0]
    assert gate.api_url().startswith('https://')
    upd,replies=update_for('/shortener status',1)
    asyncio.run(admin_bot.cmd_shortener(upd,NS(args=['status'])))
    assert 'api=offline' not in replies[0]


def test_source_signal_snapshot_reused_across_distinct_gallery_requests(env,monkeypatch):
    populate(env.primary.nhentai_cache)
    calls=[]
    coll=env.primary.nhentai_cache
    original=coll.find
    def counted(query=None,*args,**kwargs):
        calls.append(query)
        return original(query,*args,**kwargs)
    monkeypatch.setattr(coll,'find',counted)
    assert similar.similar_galleries('100',8)
    scans=sum(bool(q and isinstance(q.get('_id'),dict) and '$regex' in q['_id']) for q in calls)
    assert scans==2  # object projection + legacy-string compatibility
    assert similar.similar_galleries('101',8)
    assert sum(bool(q and isinstance(q.get('_id'),dict) and '$regex' in q['_id']) for q in calls)==scans


def test_primary_projection_never_transfers_reader_pages(env,monkeypatch):
    coll=env.primary.nhentai_cache
    for gid in range(100,104):
        p=payload(gid);p['pages_meta']=[{'path':'large'*10000}]*20
        coll.insert_one({'_id':f'gallery:{gid}','payload':p})
    query_log=[]; original=coll.find
    def logged(q=None,projection=None,*a,**k):
        query_log.append((q,projection))
        return original(q,projection,*a,**k)
    monkeypatch.setattr(coll,'find',logged)
    out=similar.similar_galleries('100',8)
    assert len(out)==3 and all(x['tags']==[] for x in out)
    object_reads=[projection for q,projection in query_log if q and q.get('payload')=={'$type':'object'}]
    assert object_reads and all('payload' not in projection for projection in object_reads)
    assert not any('pages_meta' in k for projection in object_reads for k in projection)


def test_snapshot_expires_and_new_related_gallery_is_visible(env,monkeypatch):
    populate(env.primary.nhentai_cache,3)
    assert len(similar.similar_galleries('100',8))==2
    env.primary.nhentai_cache.insert_one({'_id':'gallery:110','payload':payload(110)})
    similar._SIGNAL_INDEX['built']-=similar._SIGNAL_TTL_SEC+1
    env.primary.nhentai_cache.delete_one({'_id':'similar:v13.16:100'})
    assert len(similar.similar_galleries('100',8))==3


def test_poller_leases_single_owner_expiry_and_guarded_release(env):
    a,b=admin_bot._PollerLease('offline-token'),admin_bot._PollerLease('offline-token')
    assert a.acquire() and not b.acquire() and a.renew()
    b.release();assert not b.acquire()
    env.primary.bot0_poller_leases.update_one({'_id':a.key},{'$set':{'expires_at':time.time()-1}})
    assert b.acquire() and not a.renew()
    a.release();assert b.renew()
    b.release();assert a.acquire()


def test_poller_startup_rechecks_expired_lease_before_polling(env,monkeypatch):
    tracker=Mock();monkeypatch.setattr(admin_bot.progress_tracker,'ensure_tracker_running',tracker)
    app=NS(bot_data={'_poller_lease':NS(renew=lambda:False)},stop_running=Mock())
    asyncio.run(admin_bot._on_startup(app))
    app.stop_running.assert_called_once();tracker.assert_not_called()


def test_actual_app_gzip_and_head_health_without_turso_call(env,monkeypatch):
    from fastapi.testclient import TestClient
    from app.services import turso_client,deletion_scheduler,delivery_watcher
    ensure=Mock(side_effect=AssertionError('Turso used while disabled'))
    monkeypatch.setattr(turso_client,'ensure_schema',ensure)
    monkeypatch.setattr(deletion_scheduler,'start_background_loop',lambda:None)
    monkeypatch.setattr(delivery_watcher,'start_background_loop',lambda:None)
    spec=importlib.util.spec_from_file_location('v1316_main',ROOT/'miniapp/backend/main.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    with TestClient(mod.app) as client:
        for path in ('/','/healthz'):
            r=client.head(path);assert r.status_code==200 and not r.content
        r=client.get('/static/js/plugins/shortener-gate.js',headers={'Accept-Encoding':'gzip'})
        assert r.headers['content-encoding']=='gzip'
        assert 'no-store' in r.headers['cache-control'] and 'deep_link' not in r.text
        plain=client.get('/static/js/plugins/shortener-gate.js',headers={'Accept-Encoding':'identity'})
        assert 'content-encoding' not in plain.headers
        assert plain.content==r.content
    ensure.assert_not_called()


def test_main_secures_lease_before_build_or_polling(env,monkeypatch):
    events=[]
    lease=NS(acquire=lambda:events.append('acquire') or True,release=lambda:events.append('release'))
    monkeypatch.setattr(admin_bot,'settings',NS(admin_bot_token='offline',admin_user_id=1))
    monkeypatch.setattr(admin_bot,'_PollerLease',lambda token:lease)
    def poll(**kwargs):
        events.append('poll')
        assert not kwargs['drop_pending_updates']
    app=NS(bot_data={},run_polling=poll)
    monkeypatch.setattr(admin_bot,'build_app',lambda:events.append('build') or app)
    assert admin_bot.main()==0
    assert events==['acquire','build','poll','release']


def test_scan_doc_limit_is_shared_between_object_and_json_modes(env,monkeypatch):
    coll=env.primary.nhentai_cache
    # Empty canonical payloads must still consume the bounded scan budget.
    for i in range(6):
        coll.insert_one({'_id':f'gallery:{i}','payload':{'tag_groups':{}}})
    coll.insert_one({'_id':'gallery:200','payload':json.dumps(payload(200))})
    monkeypatch.setattr(similar,'_MAX_SCAN',4)
    rows,_=similar._signal_rows(coll)
    assert rows==[]  # not 4 object documents PLUS 4 JSON documents
