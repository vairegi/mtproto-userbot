/*
  plugins/reader.js — v12.71: in-app "Read" viewer (skip-fix).

  Webtoon-style vertical continuous scroll with progressive windowing.
  v12.71 BUG FIX (skipped pages): the old trimWindow() dropped a trimmed
  page's <img> and replaced it with an EMPTY placeholder. Scrolling back
  up never re-mounted those pages — the IntersectionObserver only watches
  the bottom sentinel and nextIdx had already advanced past them — so any
  trimmed page stayed blank forever ("1-4 blank, 5-7 load, 8-10 blank").
  Fix: mountPage() is idempotent and keyed by page number; placeholders
  keep a data-page stamp and are REUSED in place on re-mount; a new
  ensureVisibleWindow() runs on every scroll tick and mounts any unmounted
  page within ±5 of the viewport; img.onerror leaves a visible
  "tap to retry" placeholder instead of an invisible gap.

  Server cost: ZERO image bandwidth — pages hotlink nhentai's CDN exactly
  like cover cards already do. The reader only consumes detail.pages_meta
  ([{n, path, w, h}]) which the gallery endpoint now ships.
*/
import { h } from "core/components.js";

const IMG_SERVERS = ["https://i1.nhentai.net", "https://i2.nhentai.net",
                     "https://i3.nhentai.net", "https://i4.nhentai.net"];
const FIRST_CHUNK = 3;      // pages mounted on open
const STEP = 2;             // pages appended per sentinel hit
const MAX_MOUNTED = 21;     // current ±10 window + slack
const AHEAD = 5;            // pages to keep mounted below the viewport
const BEHIND = 5;           // pages to keep mounted above the viewport

let _overlay = null;        // single instance — WebView RAM safety

function _serverFor(n) { return IMG_SERVERS[(n - 1) % IMG_SERVERS.length]; }

export function pageUrl(meta) {
  return `${_serverFor(meta.n)}/${meta.path}`;
}

export function openReader(gallery) {
  const meta = (gallery && gallery.pages_meta) || [];
  const gid = (gallery && gallery.id) || "?";
  if (!meta.length) {
    // First-ever open of a legacy cached row — the backend is backfilling
    // pages_meta right now; tell the user to retry.
    try {
      var tg = (typeof window !== "undefined") ? window.Telegram : null;
      if (tg && tg.WebApp && tg.WebApp.showPopup) {
        tg.WebApp.showPopup({ title: "Loading pages",
          message: "Fetching this gallery's pages for the first time — tap Read again in a few seconds.",
          buttons: [{ type: "ok" }] });
      } else if (typeof window !== "undefined" && window.alert) {
        window.alert("Loading pages — tap Read again in a few seconds.");
      }
    } catch (_) {}
    return false;
  }
  closeReader();

  const scroller = h("div", {
    style: {
      position: "fixed", inset: "0", zIndex: "9999",
      background: "#0b0b0d", overflowY: "auto",
      WebkitOverflowScrolling: "touch", overscrollBehavior: "contain",
    },
  });
  const header = h("div", {
    style: {
      position: "sticky", top: "0", zIndex: "2", display: "flex",
      alignItems: "center", gap: "10px", padding: "10px 14px",
      background: "rgba(11,11,13,0.92)", backdropFilter: "blur(6px)",
      color: "#fff", fontSize: "13px", fontWeight: "700",
    },
  },
    h("button", {
      style: { background: "none", border: "none", color: "#fff",
               fontSize: "18px", cursor: "pointer", padding: "4px 8px" },
      onclick: closeReader,
    }, "✕"),
    h("span", { id: "reader-progress", class: "u-grow u-truncate" },
      `1 / ${meta.length}`),
  );
  const body = h("div", { style: { maxWidth: "900px", margin: "0 auto" } });
  const sentinel = h("div", { style: { height: "1px" } });
  scroller.append(header, body, sentinel);
  document.body.appendChild(scroller);
  _overlay = scroller;
  document.body.style.overflow = "hidden";

  // Mounted-window registry keyed by PAGE NUMBER so a trimmed page can be
  // re-mounted when the user scrolls back to it.
  const mountedByN = new Map();  // n -> el
  let nextIdx = 0;

  const ph = (m) => h("div", {
    dataset: { page: String(m.n) },
    style: {
      width: "100%", aspectRatio: `${m.w} / ${m.h}`,
      background: "#17171b", position: "relative", overflow: "hidden",
      color: "#3a3a44", fontSize: "12px", fontWeight: "600",
      display: "flex", alignItems: "center", justifyContent: "center",
    },
  }, `page ${m.n}`);

  function mountPage(m) {
    if (mountedByN.has(m.n)) return;
    let el = body.querySelector(`[data-page="${m.n}"]`);
    if (!el) {
      el = ph(m);
      body.insertBefore(el, null);
    }
    const img = new Image();
    img.alt = `page ${m.n}`;
    img.style.cssText = "width:100%;display:block;";
    img.onload = () => {
      el.replaceChildren(img);
      el.style.aspectRatio = "auto";
      el.style.display = "";
    };
    img.onerror = () => {
      el.textContent = `page ${m.n} — tap to retry`;
      el.style.cursor = "pointer";
      el.onclick = () => { el.onclick = null; el.textContent = ""; img.src = pageUrl(m); };
    };
    img.src = pageUrl(m);
    mountedByN.set(m.n, el);
  }

  function unmountPage(n) {
    const el = mountedByN.get(n);
    if (!el) return;
    const m = meta.find(x => x.n === n);
    if (m) {
      const fresh = ph(m);
      el.replaceWith(fresh);
    }
    mountedByN.delete(n);
  }

  function trimWindow() {
    if (mountedByN.size <= MAX_MOUNTED) return;
    const viewTop = scroller.scrollTop;
    const viewBot = viewTop + scroller.clientHeight;
    const ranked = [...mountedByN.entries()].map(([n, el]) => {
      const top = el.offsetTop, bot = top + el.offsetHeight;
      const dist = (bot < viewTop) ? (viewTop - bot)
                 : (top > viewBot) ? (top - viewBot) : 0;
      return { n, dist };
    }).sort((a, b) => b.dist - a.dist);
    const excess = mountedByN.size - MAX_MOUNTED;
    for (let i = 0; i < excess && i < ranked.length; i++)
      unmountPage(ranked[i].n);
  }

  // Re-hydrate any page inside viewport ±BEHIND/AHEAD that was trimmed.
  function ensureVisibleWindow() {
    const viewTop = scroller.scrollTop;
    const kids = body.children;
    let curIdx = 0;
    for (let i = 0; i < kids.length; i++) {
      const el = kids[i];
      if (el.offsetTop + el.offsetHeight >= viewTop) { curIdx = i; break; }
      curIdx = i;
    }
    const curN = parseInt(kids[curIdx] && kids[curIdx].dataset
      ? kids[curIdx].dataset.page : "1", 10) || 1;
    const curMetaIdx = Math.max(0, meta.findIndex(m => m.n === curN));
    const from = Math.max(0, curMetaIdx - BEHIND);
    const to   = Math.min(meta.length - 1, curMetaIdx + AHEAD);
    for (let i = from; i <= to; i++) mountPage(meta[i]);
  }

  function mountNext() {
    for (let i = 0; i < STEP && nextIdx < meta.length; i++, nextIdx++)
      mountPage(meta[nextIdx]);
    trimWindow();
    updateProgress();
  }

  function updateProgress() {
    const viewTop = scroller.scrollTop;
    const kids = body.children;
    let curN = 1;
    for (let i = 0; i < kids.length; i++) {
      const el = kids[i];
      if (el.offsetTop + el.offsetHeight >= viewTop) {
        curN = parseInt(el.dataset && el.dataset.page || "1", 10) || 1;
        break;
      }
    }
    const el = scroller.querySelector("#reader-progress");
    if (el) el.textContent = `${curN} / ${meta.length}`;
  }

  for (let i = 0; i < FIRST_CHUNK && nextIdx < meta.length; i++, nextIdx++)
    mountPage(meta[nextIdx]);
  for (let i = nextIdx; i < Math.min(nextIdx + 2, meta.length); i++)
    new Image().src = pageUrl(meta[i]);

  const io = new IntersectionObserver((entries) => {
    if (entries.some(e => e.isIntersecting)) {
      mountNext();
      for (let i = nextIdx; i < Math.min(nextIdx + 2, meta.length); i++)
        new Image().src = pageUrl(meta[i]);
    }
  }, { root: scroller, rootMargin: "600px 0px" });
  io.observe(sentinel);

  scroller.addEventListener("scroll", () => {
    ensureVisibleWindow();
    trimWindow();
    updateProgress();
  }, { passive: true });

  const onKey = (e) => { if (e.key === "Escape") closeReader(); };
  document.addEventListener("keydown", onKey, { once: true });
  return true;
}

export function closeReader() {
  if (_overlay) { _overlay.remove(); _overlay = null; }
  document.body.style.overflow = "";
}
