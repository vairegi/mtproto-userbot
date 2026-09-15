"""v4.0: similar-content recommendation engine."""
import sys, asyncio
sys.path.insert(0, "/home/user/telegram-file-bot")
import pytest
from app.services import recommend as R


CAP_A = ("**Ume to Warui Producer**\n\n➤ #670495\n\n"
         "➤ Parodies:   #the_idolmaster\n➤ Characters: #ume_hanami\n"
         "➤ Languages:  #translated #english\n➤ Categories: #doujinshi\n\n"
         "➤ Tags:       #big_breasts #sole_female #collar #kemonomimi")
CAP_B = ("**Ume no Hana**\n\n➤ #670500\n\n➤ Parodies:   #the_idolmaster\n"
         "➤ Characters: #ume_hanami\n➤ Languages:  #translated #english\n"
         "➤ Categories: #doujinshi\n\n➤ Tags:       #big_breasts #sole_female #schoolgirl")
CAP_C = ("**Totally Different Cooking Story**\n\n➤ #670600\n\n"
         "➤ Parodies:   #original\n➤ Languages:  #translated #english\n"
         "➤ Categories: #doujinshi\n\n➤ Tags:       #yaoi #males_only #cooking")
CAP_D = ("**Unrelated But Shares Tags**\n\n➤ #670700\n\n➤ Parodies:   #original\n"
         "➤ Languages:  #translated #english\n➤ Categories: #doujinshi\n\n"
         "➤ Tags:       #big_breasts #sole_female #kemonomimi")


def test_parse_tags_sections_and_exclusions():
    t = R.parse_tags(CAP_A)
    assert t["parodies"] == {"the_idolmaster"}
    assert t["characters"] == {"ume_hanami"}
    assert "big_breasts" in t["tags"]
    assert "languages" not in t and "categories" not in t  # excluded sections


def test_title_of_strips_markdown_and_arrows():
    assert R._title_of(CAP_A) == "Ume to Warui Producer"
    assert R._title_of("") == ""


def test_title_tokens_stopwords():
    toks = R.title_tokens("Ume and the Evil Producer")
    assert "and" not in toks and "the" not in toks
    assert "ume" in toks and "evil" in toks and "producer" in toks


def test_tag_similarity_weighting():
    a = R.parse_tags(CAP_A)
    b_close = R.parse_tags(CAP_B)   # same parody + character + tag overlap
    b_far = R.parse_tags(CAP_C)     # different everything
    b_tags = R.parse_tags(CAP_D)    # only generic tag overlap
    s_close = R.tag_similarity(a, b_close)
    s_far = R.tag_similarity(a, b_far)
    s_tags = R.tag_similarity(a, b_tags)
    assert s_close > s_tags > s_far
    assert s_far == 0.0


def test_similar_covers_ranking(monkeypatch):
    base = {"id": 100, "caption": CAP_A, "post_number": 2325, "code": "aaa"}
    pool = [
        {"id": 1, "caption": CAP_B, "post_number": 2324, "code": "b1"},  # title+tags match
        {"id": 2, "caption": CAP_C, "post_number": 2323, "code": "c1"},  # no match
        {"id": 3, "caption": CAP_D, "post_number": 2322, "code": "d1"},  # tag-only match
    ]
    async def _pool(limit=400, exclude_id=0):
        return [c for c in pool if c["id"] != exclude_id]
    monkeypatch.setattr(R.repo, "recent_published_covers", _pool)
    out = asyncio.run(R.similar_covers(base, limit=5))
    nums = [c["post_number"] for c in out]
    assert 2323 not in nums              # zero relevance excluded
    assert nums[0] == 2324               # title+tag match ranks first
    assert 2322 in nums                  # tag-only match still offered
    assert all("code" in c and "post_number" in c for c in out)


def test_similar_covers_empty_pool(monkeypatch):
    async def _pool(limit=400, exclude_id=0): return []
    monkeypatch.setattr(R.repo, "recent_published_covers", _pool)
    assert asyncio.run(R.similar_covers({"id": 1, "caption": CAP_A})) == []


def test_send_similar_builds_buttons(monkeypatch):
    """posting._send_similar sends one message with title buttons (deep links)."""
    from app.services import posting
    from types import SimpleNamespace
    sims = [{"post_number": 2324, "code": "b1", "caption": CAP_B},
            {"post_number": 2322, "code": "d1", "caption": CAP_D}]
    async def _sims(cover, limit=5): return sims
    async def _user(bot): return "mybot"
    monkeypatch.setattr("app.services.recommend.similar_covers", _sims)
    monkeypatch.setattr(posting, "get_bot_username", _user)
    sent = {}
    async def _tg_send(bot, chat_id, text, reply_markup=None, **kw):
        sent["text"] = text; sent["kb"] = reply_markup
        return SimpleNamespace(message_id=42)
    monkeypatch.setattr(posting.tg, "send_message", _tg_send)
    ids = []
    asyncio.run(posting._send_similar(FakeBot, 999, {"id": 1, "caption": CAP_A}, ids))
    assert "Similar Doujinshi" in sent["text"]
    btns = sent["kb"].inline_keyboard
    assert len(btns) == 3  # v4.2: +1 row for the 🔄 Refresh button
    assert "#2324" in btns[0][0].text and "get_b1" in btns[0][0].url
    assert "#2322" in btns[1][0].text and "get_d1" in btns[1][0].url
    assert ids == []  # v4.2: card has its own 60s self-destruct, not the autodelete batch


class FakeBot:  # placeholder (not used directly)
    pass
