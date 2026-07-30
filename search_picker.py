"""
search_picker.py — Interactive multi-select search picker for the admin bot.

Flow:
  /search <keyword>          → shows page 1 with ⬜ Select buttons, Prev/Next,
                                and Confirm/Cancel controls.
  User taps ⬜ Select rows    → toggles between ⬜ and ☑️ (per-gallery).
  User taps ⬅️ Prev / Next ➡️ → same session, different page, selections
                                on other pages are remembered.
  User taps ✅ Confirm (N)    → all N selected URLs go through queue_service
                                .enqueue_batch (same as /fetch), with
                                already-uploaded / already-queued detection.
  User taps ❌ Cancel        → session discarded, buttons removed.

State is held in-process (application.bot_data["search_sessions"]). If the
admin bot restarts, any open pickers become inert (their buttons will show
"Session expired"). The user just runs /search again.
"""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import hf_scraper
from logging_setup import setup_logging
from queue_service import enqueue_batch
import db
import progress_tracker

log = setup_logging("search_picker")

RESULTS_PER_PAGE = 8
SESSION_TTL_SEC = 30 * 60  # 30 min


# ------------------------- data model -------------------------

@dataclass
class Hit:
    gallery_id: str
    title: str
    url: str


@dataclass
class Session:
    session_id: str
    owner_user_id: int
    query: str
    current_page: int = 1
    total_results: int = 0
    pages_cache: Dict[int, List[Hit]] = field(default_factory=dict)  # page_num -> hits
    selected: Dict[str, Hit] = field(default_factory=dict)           # gallery_id -> Hit
    queued: set = field(default_factory=set)                         # gallery_ids already sent to queue
    created_at: float = field(default_factory=time.time)

    @property
    def total_pages(self) -> int:
        if self.total_results <= 0:
            return 1
        # hentaifox returns 20-per-page but we display 8 (we may re-slice or
        # just page through the 20-per-page source).  We rely on has_next
        # from the scraper for the real "is there more" answer; total_pages
        # is only a display hint.
        return max(1, (self.total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)


def _sessions(ctx: ContextTypes.DEFAULT_TYPE) -> Dict[str, Session]:
    if "search_sessions" not in ctx.application.bot_data:
        ctx.application.bot_data["search_sessions"] = {}
    return ctx.application.bot_data["search_sessions"]


def _gc_sessions(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    now = time.time()
    ss = _sessions(ctx)
    stale = [sid for sid, s in ss.items() if now - s.created_at > SESSION_TTL_SEC]
    for sid in stale:
        ss.pop(sid, None)


# ------------------------- fetching pages -------------------------

async def _load_page(sess: Session, page_num: int) -> List[Hit]:
    """Return the hits for `page_num`, caching hentaifox pages as needed.

    hentaifox natively serves 20 results per page. We re-slice into windows
    of RESULTS_PER_PAGE (8).
    """
    if page_num in sess.pages_cache:
        return sess.pages_cache[page_num]

    # Map our display page -> hentaifox page + slice offset
    # display page 1 -> hf page 1, slice [0:8]
    # display page 2 -> hf page 1, slice [8:16]
    # display page 3 -> hf page 1, slice [16:20] + hf page 2, slice [0:4]  (spanning is complex)
    # Simplification: fetch 3 hf pages at once if needed and re-slice.
    hits_all: List[Hit] = []
    start_idx = (page_num - 1) * RESULTS_PER_PAGE
    end_idx = start_idx + RESULTS_PER_PAGE

    # HF serves 20 per page. Fetch as many as needed.
    hf_page = 1
    while len(hits_all) < end_idx:
        result = await hf_scraper.search(sess.query, page=hf_page)
        if result is None or not result.hits:
            break
        if sess.total_results == 0:
            sess.total_results = result.total_results
        for h in result.hits:
            hits_all.append(Hit(gallery_id=h.gallery_id, title=h.title, url=h.url))
        if not result.has_next:
            break
        hf_page += 1
        if hf_page > 50:  # safety
            break

    window = hits_all[start_idx:end_idx]
    sess.pages_cache[page_num] = window
    return window


# ------------------------- keyboard rendering -------------------------

def _short_title(title: str, limit: int = 40) -> str:
    t = title.strip()
    if len(t) <= limit:
        return t
    return t[: limit - 1] + "…"


def _build_keyboard(sess: Session, hits: List[Hit]) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for h in hits:
        gid = h.gallery_id
        if gid in sess.queued:
            label = f"✓ Added — {_short_title(h.title, 34)}"
            # dead button: use a no-op callback that we ignore
            rows.append([InlineKeyboardButton(label, callback_data=f"sp|{sess.session_id}|noop|{gid}")])
        else:
            mark = "☑️" if gid in sess.selected else "⬜"
            label = f"{mark} {_short_title(h.title, 38)}"
            rows.append([InlineKeyboardButton(label, callback_data=f"sp|{sess.session_id}|t|{gid}")])

    # Nav row
    nav: List[InlineKeyboardButton] = []
    if sess.current_page > 1:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sp|{sess.session_id}|p|0"))
    nav.append(InlineKeyboardButton(f"Page {sess.current_page}", callback_data=f"sp|{sess.session_id}|noop|0"))
    # Show Next only if we suspect there's more content. Easiest: if we filled
    # RESULTS_PER_PAGE on this window OR total_results indicates more pages remain.
    remaining = sess.total_results - sess.current_page * RESULTS_PER_PAGE
    if remaining > 0 or len(hits) == RESULTS_PER_PAGE:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"sp|{sess.session_id}|n|0"))
    rows.append(nav)

    # Action row
    sel_count = len(sess.selected)
    action: List[InlineKeyboardButton] = [
        InlineKeyboardButton(
            f"✅ Confirm ({sel_count})",
            callback_data=f"sp|{sess.session_id}|c|0",
        ),
        InlineKeyboardButton("❌ Cancel", callback_data=f"sp|{sess.session_id}|x|0"),
    ]
    rows.append(action)

    return InlineKeyboardMarkup(rows)


def _build_header(sess: Session) -> str:
    lines = [
        f'🔎 Search: "{sess.query}"  ({sess.total_results:,} results)',
        f"Page {sess.current_page} — tap ⬜ to select, then ✅ Confirm.",
    ]
    return "\n".join(lines)


def _build_body(hits: List[Hit], sess: Session) -> str:
    """Compact numbered list below the header showing titles + links."""
    if not hits:
        return "(no more results)"
    lines = []
    for i, h in enumerate(hits, start=1):
        marker = "✓" if h.gallery_id in sess.queued else ("☑" if h.gallery_id in sess.selected else "·")
        lines.append(f"{marker} #{h.gallery_id}  {_short_title(h.title, 60)}")
        lines.append(f"    {h.url}")
    return "\n".join(lines)


# ------------------------- public entry points -------------------------

async def start_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE, query: str) -> None:
    """Called from cmd_search in admin_bot.py."""
    msg = update.effective_message
    if not msg:
        return
    _gc_sessions(ctx)

    sess = Session(
        session_id=secrets.token_urlsafe(6),
        owner_user_id=int(update.effective_user.id),
        query=query.strip(),
    )
    _sessions(ctx)[sess.session_id] = sess

    hits = await _load_page(sess, 1)
    if not hits:
        # Detect: was it a hard failure (network) or empty results?
        if sess.total_results == 0 and not sess.pages_cache.get(1):
            # Distinguish "search unavailable" vs "no results"
            probe = await hf_scraper.search(sess.query, page=1)
            if probe is None:
                await msg.reply_text("Search unavailable, try again later.")
                _sessions(ctx).pop(sess.session_id, None)
                return
        await msg.reply_text(f'No results for "{sess.query}".')
        _sessions(ctx).pop(sess.session_id, None)
        return

    text = _build_header(sess) + "\n\n" + _build_body(hits, sess)
    keyboard = _build_keyboard(sess, hits)
    await msg.reply_text(text[:4000], reply_markup=keyboard, disable_web_page_preview=True)


async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles every `sp|...` callback query from the picker keyboards."""
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("sp|"):
        return
    parts = q.data.split("|", 3)
    if len(parts) < 4:
        await q.answer()
        return
    _, sid, action, arg = parts

    sess = _sessions(ctx).get(sid)
    if sess is None:
        await q.answer("Session expired — run /search again.", show_alert=True)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:  # noqa: BLE001
            pass
        return

    # Only the person who ran /search can drive its buttons
    if int(q.from_user.id) != int(sess.owner_user_id):
        await q.answer("This search belongs to another admin.", show_alert=True)
        return

    if action == "noop":
        await q.answer()
        return

    if action == "t":  # toggle select
        gid = arg
        # Find the Hit object by scanning the cache
        target: Optional[Hit] = None
        for hits in sess.pages_cache.values():
            for h in hits:
                if h.gallery_id == gid:
                    target = h
                    break
            if target:
                break
        if target is None:
            await q.answer()
            return
        if gid in sess.queued:
            await q.answer("Already added.")
            return
        if gid in sess.selected:
            sess.selected.pop(gid, None)
        else:
            sess.selected[gid] = target
        await _refresh_message(q, ctx, sess)
        await q.answer()
        return

    if action == "n":
        # Load next page
        next_page = sess.current_page + 1
        hits = await _load_page(sess, next_page)
        if not hits:
            await q.answer("No more results.")
            return
        sess.current_page = next_page
        await _refresh_message(q, ctx, sess)
        await q.answer()
        return

    if action == "p":
        if sess.current_page <= 1:
            await q.answer()
            return
        sess.current_page -= 1
        await _refresh_message(q, ctx, sess)
        await q.answer()
        return

    if action == "x":
        _sessions(ctx).pop(sid, None)
        try:
            await q.edit_message_text(
                f'🔎 Search cancelled: "{sess.query}"',
                reply_markup=None,
            )
        except Exception:  # noqa: BLE001
            pass
        await q.answer("Cancelled.")
        return

    if action == "c":
        if not sess.selected:
            await q.answer("Nothing selected. Tap ⬜ next to a title first.", show_alert=True)
            return
        chosen: List[Hit] = list(sess.selected.values())

        # ---- v11 token gating (Q3a: block whole batch if not enough tokens) ----
        user_id = int(q.from_user.id)
        uname = q.from_user.username or ""
        from config import settings as _s
        is_admin_user = False
        conn0 = db.connect()
        try:
            is_admin_user = (
                user_id == int(_s.admin_user_id)
                or db.get_admin(conn0, user_id) is not None
            )
        finally:
            conn0.close()

        need = len(chosen)
        if not is_admin_user:
            conn0 = db.connect()
            try:
                tok_info = db.get_user_tokens(conn0, user_id, uname or None)
            finally:
                conn0.close()
            if tok_info["remaining"] < need:
                await q.answer(
                    f"Only {tok_info['remaining']} token(s) left today. "
                    f"Select at most that many, or wait for daily reset.",
                    show_alert=True,
                )
                return
            conn0 = db.connect()
            try:
                ok = db.consume_tokens(conn0, user_id, need, uname or None)
            finally:
                conn0.close()
            if not ok:
                await q.answer("Not enough tokens. Please retry with fewer items.", show_alert=True)
                return

        # Commit: clear selection so re-Confirm won't double-queue
        sess.selected.clear()

        payload = "\n".join(h.url for h in chosen)
        try:
            max_links = int(getattr(_s, "batch_max_links", 25))
        except Exception:  # noqa: BLE001
            max_links = 25
        result = enqueue_batch(
            payload,
            max_links=max_links,
            via_search=True,
            submitted_by=user_id,
            username=uname or None,
            chat_id=q.message.chat_id if q.message else None,
        )
        # Refund any items that got skipped/rejected before consuming a token
        if not is_admin_user:
            skipped = (
                len(result.rejected)
                + len(result.skipped_duplicates)
                + len(result.skipped_already_pending)
                + len(result.skipped_already_done)
            )
            if skipped > 0:
                conn0 = db.connect()
                try:
                    db.refund_token(conn0, user_id, skipped)
                finally:
                    conn0.close()

        # Mark queued ones so their buttons turn into ✓ Added
        queued_urls = {u for _, u in result.queued}
        already_done_urls = set(result.skipped_already_done)
        already_pending_urls = set(result.skipped_already_pending)
        for h in chosen:
            # Match on the normalised URL. queue_service stores the same string
            # our Hit carries (trailing slash + lowercase host may differ, so
            # accept a loose match).
            if _url_in(h.url, queued_urls) or _url_in(h.url, already_pending_urls):
                sess.queued.add(h.gallery_id)
            elif _url_in(h.url, already_done_urls):
                # Also grey it out — it's already in the main channel.
                sess.queued.add(h.gallery_id)

        # ------------------------------------------------------------------
        # Build the reply and (when there are queued items) hand it off to
        # the progress tracker so it LIVE-EDITS this same message. Prior
        # versions sent a separate confirm reply AND then let the tracker
        # post its own progress message — users saw two nearly-identical
        # messages back-to-back. Now everything goes into one consolidated
        # message: fixed header, live progress lines, fixed token line, and
        # the channel footer (rendered by progress_tracker).
        # ------------------------------------------------------------------

        # Header lines — the static summary that stays visible above the
        # live progress list.
        header_lines = [f"✅ /search → queue for \"{sess.query}\":"]
        header_lines.append(f"  {len(result.queued)} queued")
        if result.skipped_already_done:
            header_lines.append(
                f"  {len(result.skipped_already_done)} skipped — already uploaded to channel:"
            )
            for u in result.skipped_already_done[:10]:
                header_lines.append(f"    • {u}")
        if result.skipped_already_pending:
            header_lines.append(
                f"  {len(result.skipped_already_pending)} skipped — already in queue:"
            )
            for u in result.skipped_already_pending[:10]:
                header_lines.append(f"    • {u}")
        if result.rejected:
            header_lines.append(f"  {len(result.rejected)} rejected:")
            for line, why in result.rejected[:10]:
                header_lines.append(f"    • {line} — {why}")
        header_text = "\n".join(header_lines)

        # Token line — shown to non-admins only, placed between the progress
        # list and the channel footer inside the consolidated message.
        token_line = ""
        if not is_admin_user:
            conn0 = db.connect()
            try:
                tok_now = db.get_user_tokens(conn0, user_id, uname or None)
            finally:
                conn0.close()
            if tok_now["remaining"] <= 3:
                token_line = (
                    f"⚠️ You have {tok_now['remaining']}/{tok_now['daily_cap']} tokens left today."
                )
            else:
                token_line = (
                    f"🎟 Tokens: {tok_now['remaining']}/{tok_now['daily_cap']} remaining today."
                )

        if result.queued:
            # Send the confirm reply as the SEED for the progress message,
            # then hand its message_id to the tracker so subsequent edits
            # land on the same bubble. The tracker's footer already contains
            # the posting-channel + daily-updates URLs, so we don't append
            # them here.
            reply_msg = await q.message.reply_text(header_text[:4000])
            try:
                await progress_tracker.start_batch_tracking(
                    ctx.application, q.message.chat_id, result.queued,
                    header=header_text,
                    token_line=token_line,
                    existing_message_id=reply_msg.message_id,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("progress tracker failed to start for /search batch: %s", e)
        else:
            # Nothing queued (everything was skipped/rejected). Send a
            # one-shot reply that still shows the token line + channel
            # footer, since there will be no live progress message to carry
            # them.
            single_lines = [header_text]
            if token_line:
                single_lines.append("")
                single_lines.append(token_line)
            single_lines.append("")
            single_lines.append("📢 Posting in this Channel: https://t.me/+M6yURQt1-TY1YTZl")
            single_lines.append("📣 Daily Updates Here — https://t.me/+uyNxVAVPdUBlOWU9")
            await q.message.reply_text("\n".join(single_lines)[:4000])

        await _refresh_message(q, ctx, sess)
        await q.answer("Queued.")
        return

    # unknown action
    await q.answer()


def _url_in(url: str, bag: set) -> bool:
    """Loose URL match: also compare with/without trailing slash + lowercase."""
    if url in bag:
        return True
    v = url.rstrip("/")
    if v in bag or (v + "/") in bag:
        return True
    v2 = url.lower().rstrip("/")
    for b in bag:
        if b.lower().rstrip("/") == v2:
            return True
    return False


async def _refresh_message(q, ctx: ContextTypes.DEFAULT_TYPE, sess: Session) -> None:
    hits = await _load_page(sess, sess.current_page)
    text = _build_header(sess) + "\n\n" + _build_body(hits, sess)
    keyboard = _build_keyboard(sess, hits)
    try:
        await q.edit_message_text(
            text[:4000],
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except Exception as e:  # noqa: BLE001
        # Common: "Message is not modified" — ignore
        log.debug("refresh_message edit failed (ignored): %s", e)
