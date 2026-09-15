"""v4.3: VPLINK shortener gate — token lifecycle, TTL, gate send, verify
redemption + auto-delivery, landing page, admin commands."""
import sys, asyncio, time
from types import SimpleNamespace
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import repo, shortener as sh, posting
from app.handlers import shortener_cmds, setup_cmds
import app.main as main_mod


@pytest.fixture(autouse=True)
def clean_state():
    sh._TOKENS.clear()
    yield
    sh._TOKENS.clear()


@pytest.fixture
def fake_settings(monkeypatch):
    store = {}
    async def _gs(key, default=None): return store.get(key, default)
    async def _ss(key, val):
        if val is None: store.pop(key, None)
        else: store[key] = val
    async def _gsj(key, default=None): return store.get(key, default)
    async def _ssj(key, val): store[key] = val
    async def _gsb(key, default=False):
        v = store.get(key)
        return default if v is None else str(v) in ("1", "true", "on", "yes")
    monkeypatch.setattr(repo, "get_setting", _gs)
    monkeypatch.setattr(repo, "set_setting", _ss)
    monkeypatch.setattr(repo, "get_setting_json", _gsj)
    monkeypatch.setattr(repo, "set_setting_json", _ssj)
    monkeypatch.setattr(repo, "get_setting_bool", _gsb)
    monkeypatch.setattr(repo, "_mongo", lambda: False)
    return store


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---- toggle / settings ----
def test_default_off_and_ttl(fake_settings):
    assert run(sh.is_enabled()) is False
    assert run(sh.get_ttl_hours()) == 6.0


def test_ttl_bounds(fake_settings):
    run(repo.set_setting("verify_ttl_hours", "12")); assert run(sh.get_ttl_hours()) == 12.0
    run(repo.set_setting("verify_ttl_hours", "999")); assert run(sh.get_ttl_hours()) == 6.0
    run(repo.set_setting("verify_ttl_hours", "junk")); assert run(sh.get_ttl_hours()) == 6.0


# ---- token lifecycle: strict, one-time, user-bound, 30-min expiry ----
def test_token_strict_one_time_user_bound(fake_settings):
    tok = sh.new_token(7, "abc")
    assert sh.peek_token(tok)["user_id"] == 7
    assert run(sh.consume_token(tok, 999)) is None          # wrong user
    assert sh.peek_token(tok) is None                       # popped = one-time
    tok2 = sh.new_token(7, "abc")
    assert run(sh.consume_token(tok2, 7)) == "abc"
    assert run(sh.is_verified(7)) is True
    assert run(sh.consume_token(tok2, 7)) is None           # replay rejected


def test_token_expires_30min(fake_settings):
    tok = sh.new_token(7, "abc")
    sh._TOKENS[tok]["expires"] = time.monotonic() - 1       # force-expired
    assert run(sh.consume_token(tok, 7)) is None
    assert run(sh.is_verified(7)) is False


def test_unlock_expiry_uses_ttl(fake_settings):
    run(repo.set_setting("verify_ttl_hours", "1"))
    tok = sh.new_token(7, "abc")
    run(sh.consume_token(tok, 7))
    assert run(sh.is_verified(7)) is True
    # v4.3.1: stamp lives in the DB (JSON-settings fallback here) — expire it
    run(repo.verified_set(7, time.time() - 1))
    assert run(sh.is_verified(7)) is False
    # restart-proof: a NEW repo read (no RAM state) still sees the stamp
    run(repo.verified_set(7, time.time() + 3600))
    assert run(sh.is_verified(7)) is True


# ---- gate send: hardcoded primary button, fail-open, skip when verified ----
def _gate_env(monkeypatch, fake_settings, enabled=True, verified=False):
    monkeypatch.setattr(repo, "is_admin", lambda uid: asyncio.sleep(0, result=False))
    run(repo.set_setting("shortener_enabled", "1" if enabled else None))
    run(repo.set_setting("shortener_api", "https://vplink.in/api?api=T&url="))
    if verified:
        run(repo.verified_set(7, time.time() + 3600))
    async def _short(dest): return "https://vplink.in/XYZ"
    async def _uname(bot): return "mybot"
    monkeypatch.setattr(sh, "make_short_url", _short)
    monkeypatch.setattr(posting, "get_bot_username", _uname)
    sent = {}
    class _Bot:
        async def send_message(self, chat_id, text, reply_markup=None, **kw):
            sent.update(text=text, kb=reply_markup, chat=chat_id)
    return sent, _Bot()


def test_gate_message_primary_button_hardcoded(monkeypatch, fake_settings):
    sent, bot = _gate_env(monkeypatch, fake_settings)
    monkeypatch.setattr(main_mod.settings, "base_webhook_url", "https://b.onrender.com")
    assert run(sh.send_gate(bot, 7, "abc")) is True
    kb = sent["kb"].inline_keyboard
    assert kb[0][0].text == "🔓 Verify & Unlock"
    assert kb[0][0].url == "https://vplink.in/XYZ"
    assert kb[-1][0].text == "✅ I've Verified — Continue"
    assert "start=verify_" in kb[-1][0].url


def test_gate_skipped_when_verified(monkeypatch, fake_settings):
    sent, bot = _gate_env(monkeypatch, fake_settings, verified=True)
    assert run(sh.send_gate(bot, 7, "abc")) is False
    assert "kb" not in sent


def test_gate_fails_open_without_api(monkeypatch, fake_settings):
    run(repo.set_setting("shortener_enabled", "1"))         # api NOT set
    assert run(sh.send_gate(SimpleNamespace(), 7, "abc")) is False


def test_gate_secondary_buttons_below_primary(monkeypatch, fake_settings):
    run(repo.set_setting_json("shortener_buttons", [["💎 Premium", "https://t.me/p"]]))
    sent, bot = _gate_env(monkeypatch, fake_settings)
    monkeypatch.setattr(main_mod.settings, "base_webhook_url", "https://b.onrender.com")
    run(sh.send_gate(bot, 7, "abc"))
    rows = sent["kb"].inline_keyboard
    assert rows[0][0].text == "🔓 Verify & Unlock"          # primary on top
    assert rows[1][0].text == "💎 Premium"                  # secondary below


# ---- /start=verify_ redemption + auto-delivery ----
def test_verify_deep_link_verifies_and_auto_delivers(monkeypatch, fake_settings):
    async def _no_admin(uid): return False
    monkeypatch.setattr(setup_cmds.repo, "is_admin", _no_admin)
    async def _noop(*a, **k): return None
    monkeypatch.setattr(setup_cmds.repo, "track_user_seen", _noop)
    monkeypatch.setattr(setup_cmds.repo, "upsert_directory_user", _noop)
    async def _no_bootstrap(uid): return None
    monkeypatch.setattr(setup_cmds, "_bootstrap_super", _no_bootstrap)
    monkeypatch.setattr(setup_cmds, "_track_user", _noop)
    tok = sh.new_token(7, "abc")
    cover = {"id": 1, "kind": "cover", "code": "abc", "post_number": 9}
    async def _cover(code): return cover
    delivered = {}
    async def _deliver(bot, uid, c):
        delivered.update(uid=uid, code=c["code"]); return {"ok": True}
    monkeypatch.setattr(setup_cmds.repo, "get_post_by_code", _cover)
    monkeypatch.setattr(setup_cmds.posting, "deliver_to_user", _deliver)
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    cmd = SimpleNamespace(args=f"verify_{tok}")
    m = SimpleNamespace(from_user=SimpleNamespace(id=7, username="u", first_name="F"),
                        reply=_reply, text=f"/start verify_{tok}")
    loop = asyncio.new_event_loop()
    loop.run_until_complete(setup_cmds.cmd_start_deep(m, SimpleNamespace(), cmd))
    loop.close()
    assert delivered == {"uid": 7, "code": "abc"}          # auto-delivered
    assert any("Verified" in r or "✅" in r for r in replies)
    assert run(sh.is_verified(7)) is True


# ---- landing page ----
def test_landing_html_autoredirect_and_button():
    html = sh.landing_html("https://t.me/mybot?start=verify_X", "Verification required")
    assert 'http-equiv="refresh"' in html                   # auto-redirect
    assert "https://t.me/mybot?start=verify_X" in html
    assert "Continue in Telegram" in html                   # fallback button
    assert "<script>" in html


# ---- admin commands ----
def test_admin_commands_toggle_and_buttons(monkeypatch, fake_settings):
    monkeypatch.setattr(shortener_cmds, "_reject_non_admin",
                        lambda m: asyncio.sleep(0, result=False))
    replies = []
    async def _reply(t, **kw): replies.append(str(t))
    def call(text):
        m = SimpleNamespace(text=text, from_user=SimpleNamespace(id=1), reply=_reply)
        verb = text.split()[0][1:]
        fn = {"shortener": shortener_cmds.cmd_shortener,
              "shortenerapi": shortener_cmds.cmd_shortenerapi,
              "setverifytime": shortener_cmds.cmd_setverifytime,
              "shortenerbtn": shortener_cmds.cmd_shortenerbtn,
              "clearshortenerbtns": shortener_cmds.cmd_clearshortenerbtns}[verb]
        run(fn(m))
    call("/shortener on")
    assert run(sh.is_enabled()) is True
    call("/shortenerapi https://vplink.in/api?api=T&url=")
    assert run(sh.get_api_base()) == "https://vplink.in/api?api=T&url="
    call("/setverifytime 12")
    assert run(sh.get_ttl_hours()) == 12.0
    call("/shortenerbtn 💎 Premium | https://t.me/p")
    assert run(sh.get_secondary_buttons()) == [["💎 Premium", "https://t.me/p"]]
    call("/clearshortenerbtns")
    assert run(sh.get_secondary_buttons()) == []
    call("/shortener off")
    assert run(sh.is_enabled()) is False
