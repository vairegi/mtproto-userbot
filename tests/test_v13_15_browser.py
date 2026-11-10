"""Real Chromium / mobile viewport smoke tests of Bot 0's actual frontend.
Offline API/Telegram/CDN fixtures; no production messages or upstream requests.
Run: python -m pytest -q tests/test_v13_15_browser.py
Extra test dependencies: pytest, playwright; playwright install chromium.
"""
import base64
import json
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright

FRONTEND=Path(__file__).resolve().parents[1]/'miniapp/frontend'
PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


@pytest.fixture
def ui():
    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,args=['--no-sandbox'])
        page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True)
        state={'locked':True,'calls':[],'status_fail':False,'delivery':True,'expire_on_action':False}
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.add_init_script('''window.Telegram={WebApp:{
          initData:'offline-signed-fixture',initDataUnsafe:{user:{id:2}},colorScheme:'dark',themeParams:{},
          ready(){},expand(){},onEvent(){},openLink(url){window.__opened=url;},
          BackButton:{show(){},hide(){},onClick(){},offClick(){}},HapticFeedback:{impactOccurred(){},notificationOccurred(){}}
        }};''')
        gallery={'id':'100','title':'Synthetic test gallery','cover':'/cover.png','pages':1,
          'tags':[], 'groups':{},'v2_status':{'known':True,'status':'COMPLETED'},
          'pages_meta':[{'n':1,'path':'offline.png','w':1,'h':1}]}
        def handler(route):
            url=urlparse(route.request.url);path=url.path
            if path.startswith('/api/'):
                state['calls'].append((route.request.method,path,url.query))
                data={}
                if path=='/api/profile/me': data={'user_id':2,'is_admin':False,'public_mode':True,'can_queue':True}
                elif path=='/api/popup': data={'show':False}
                elif path=='/api/online': data={'online':1}
                elif path=='/api/shortener/status':
                    if state['status_fail']:
                        return route.fulfill(status=503,content_type='application/json',body='{}')
                    data={'locked':state['locked'],'message':'Verification required for Read or Download',
                          'buttons':[{'label':'Tutorial','url':'https://example.invalid/tutorial'}]}
                elif path=='/api/shortener/link':
                    if state.get('link_fail'):
                        return route.fulfill(status=503,content_type='application/json',body=json.dumps({'detail':{'code':'shortener_unavailable','message':'Verification link unavailable. Access remains locked.'}}))
                    data=({'deep_link':'https://t.me/OfflineBot?start=verify_SECRET'} if state.get('legacy_link') else {'url':'https://example.invalid/verify'})
                elif path=='/api/gallery/100': data={k:v for k,v in gallery.items() if k!='pages_meta'}
                elif path=='/api/gallery/100/status': data=gallery['v2_status']
                elif path=='/api/gallery/100/suggestions': data={'items':[dict(gallery,id='101',title='Related fixture')],'count':1}
                elif path=='/api/gallery/100/read':
                    if state['locked'] or state['expire_on_action']:
                        state['locked']=True
                        return route.fulfill(status=403,content_type='application/json',body=json.dumps({'detail':{'code':'shortener_required','message':'Verification required'}}))
                    data=gallery
                elif path=='/api/queue/deliver/100':
                    if state['expire_on_action']:
                        state['locked']=True
                        return route.fulfill(status=403,content_type='application/json',body=json.dumps({'detail':{'code':'shortener_required','message':'Verification required'}}))
                    data={'ok':True,'delivered':state['delivery'],'message':'Join the required channel(s)' if not state['delivery'] else ''}
                elif path.startswith('/api/bookmarks/count/'): data={'count':0}
                elif path=='/api/search': data={'items':[gallery],'results':[gallery],'total':1,'has_more':False}
                elif path.startswith('/api/trending/'): data={'items':[],'tags':[]}
                elif path=='/api/recommendations': data={'items':[]}
                return route.fulfill(status=200,content_type='application/json',body=json.dumps(data))
            if path=='/' and url.hostname=='bot0.test':
                return route.fulfill(content_type='text/html',body=(FRONTEND/'index.html').read_text())
            if path.startswith('/static/'):
                f=FRONTEND/path.removeprefix('/static/')
                if f.is_file(): return route.fulfill(path=str(f),content_type=mimetypes.guess_type(str(f))[0] or 'text/plain')
            if path.endswith(('.png','.webp','.jpg')): return route.fulfill(content_type='image/png',body=PNG)
            # Empty external SDK/polyfill: modern Chromium uses native import maps.
            return route.fulfill(content_type='application/javascript',body='')
        page.route('**/*',handler)
        page.goto('http://bot0.test/')
        page.wait_for_function('document.querySelector("#app-main").dataset.booted === "1"')
        page.wait_for_timeout(150)
        yield page,state,errors,gallery
        browser.close()


def sheet(page,gallery):
    page.evaluate('''async g => {
      const m=await import('/static/js/plugins/detail-sheet.js');
      window.__sheet=m.openGalleryDetail(g,{is_admin:false,can_queue:true});
    }''',gallery)
    page.wait_for_selector('button:has-text("Read")')


def calls(state,path): return [c for c in state['calls'] if c[1]==path]


def test_boot_browse_recommendations_do_not_prompt(ui):
    page,state,errors,gallery=ui
    assert not calls(state,'/api/shortener/status')
    assert page.locator('.sg-overlay').count()==0
    sheet(page,gallery)
    page.wait_for_selector('text=Similar to this')
    page.wait_for_selector('text=Related fixture')
    assert not calls(state,'/api/shortener/status')
    assert not errors,errors


@pytest.mark.parametrize('action',['Read','Download Now'])
def test_action_prompts_cancel_resumes_browsing_without_side_effects(ui,action):
    page,state,errors,gallery=ui;sheet(page,gallery)
    page.get_by_role('button',name=action,exact=False).first.click()
    page.wait_for_selector('.sg-overlay')
    assert calls(state,'/api/shortener/status')[-1][2]=='prompt=true'
    assert not calls(state,'/api/gallery/100/read')
    assert not calls(state,'/api/queue/deliver/100')
    page.get_by_role('button',name='Close verification and continue browsing').click()
    assert page.locator('.sg-overlay').count()==0
    assert page.locator('body.sg-locked').count()==0
    assert page.get_by_role('button',name='Read',exact=False).first.is_visible()
    assert not errors,errors


def test_verification_link_poll_auto_close_then_reader(ui):
    page,state,errors,gallery=ui;sheet(page,gallery)
    page.get_by_role('button',name='Read',exact=False).first.click()
    page.wait_for_selector('.sg-overlay')
    page.get_by_role('button',name='Verify & Unlock',exact=False).click()
    page.wait_for_function("window.__opened === 'https://example.invalid/verify'")
    state['locked']=False
    page.wait_for_selector('.sg-overlay',state='detached',timeout=6000)
    assert not calls(state,'/api/gallery/100/read')  # no surprise action after unlock
    page.get_by_role('button',name='Read',exact=False).first.click()
    page.wait_for_selector('#reader-progress')
    assert calls(state,'/api/gallery/100/read')
    assert not errors,errors


def test_unlocked_download_delivers_and_does_not_prompt(ui):
    page,state,errors,gallery=ui;state['locked']=False;sheet(page,gallery)
    page.get_by_role('button',name='Download Now',exact=False).first.click()
    page.wait_for_selector('text=Sent to your DM')
    assert calls(state,'/api/queue/deliver/100')
    assert page.locator('.sg-overlay').count()==0
    assert not errors,errors


def test_delivery_block_is_not_reported_as_sent(ui):
    page,state,errors,gallery=ui;state['locked']=False;state['delivery']=False;sheet(page,gallery)
    page.get_by_role('button',name='Download Now',exact=False).first.click()
    page.wait_for_selector('text=Join the required channel(s)')
    assert page.get_by_text('Sent to your DM').count()==0
    assert not errors,errors


def test_ttl_expiry_between_click_and_post_reopens_gate(ui):
    page,state,errors,gallery=ui;state['locked']=False;state['expire_on_action']=True;sheet(page,gallery)
    page.get_by_role('button',name='Download Now',exact=False).first.click()
    page.wait_for_selector('.sg-overlay')
    assert calls(state,'/api/queue/deliver/100')
    assert page.get_by_text('Sent to your DM').count()==0
    assert not errors,errors


def test_read_back_then_download_stays_locked_exact_reported_sequence(ui):
    page,state,errors,gallery=ui;sheet(page,gallery)
    page.get_by_role('button',name='Read',exact=False).first.click()
    page.wait_for_selector('.sg-overlay')
    page.get_by_role('button',name='Close verification and continue browsing').click()
    page.get_by_role('button',name='Download Now',exact=False).first.click()
    page.wait_for_selector('.sg-overlay')
    assert not calls(state,'/api/gallery/100/read')
    assert not calls(state,'/api/queue/deliver/100')
    assert not errors,errors


@pytest.mark.parametrize('failure',['link_fail','legacy_link','status_fail'])
def test_provider_or_status_failure_does_not_open_raw_verify_link(ui,failure):
    page,state,errors,gallery=ui;state[failure]=True;sheet(page,gallery)
    page.get_by_role('button',name='Read',exact=False).first.click()
    page.wait_for_selector('.sg-overlay')
    if failure!='status_fail':
        page.get_by_role('button',name='Verify & Unlock',exact=False).click()
        page.wait_for_function("document.querySelector('[role=alert]').textContent.length > 0")
        assert page.evaluate('window.__opened || null') is None
    assert not calls(state,'/api/gallery/100/read')
    assert not calls(state,'/api/queue/deliver/100')
    assert page.locator('.sg-overlay').count()==1
    page.get_by_role('button',name='Close verification and continue browsing').click()
    assert page.locator('body.sg-locked').count()==0
    assert not errors,errors
