/*
  plugins/reader.js — v12.66: in-app "Read" viewer.

  Webtoon-style vertical continuous scroll with progressive windowing:
    - open        -> mount pages 1..3 (sized shimmer placeholders from the
                     API's real width/height — zero layout jump)
    - sentinel    -> IntersectionObserver at the window edge appends +2
                     pages per scroll step
    - window cap  -> max ±10 mounted pages; far-away pages swap back to
                     sized placeholders (Telegram WebViews are RAM-weak)
    - preload     -> 2 images ahead into browser cache for smooth scroll

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

let _overlay = null;        // single instance — WebView RAM safety

function _serverFor(n) { return IMG_SERVERS[(n - 1) % IMG_SERVERS.length]; }

export function pageUrl(meta) {
  return `${_serverFor(meta.n)}/${meta.path}`;
}

export function openReader(gallery) {
  const meta = (gallery && gallery.pages_meta) || [];
  const gid = (gallery && gallery.id) || "?";
  if (!meta.length) return false;
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

  let mounted = [];   // [{n, el}]
  let nextIdx = 0;    // next page index (0-based) to mount

  const ph = (m) => h("div", {
    dataset: { page: String(m.n) },
    style: {
      width: "100%", aspectRatio: `${m.w} / ${m.h}`,
      background: "#17171b", position: "relative", overflow: "hidden",
    },
  });

  function mountPage(m) {
    const el = ph(m);
    body.insertBefore(el, null);
    mounted.push({ n: m.n, el });
    const img = new Image();
    img.loading = "lazy";
    img.alt = `page ${m.n}`;
    img.style.cssText = "width:100%;display:block;";
    img.onload = () => { el.replaceChildren(img); el.style.aspectRatio = "auto"; };
    img.onerror = () => { el.textContent = ""; };
    img.src = pageUrl(m);
  }

  function mountNext() {
    for (let i = 0; i < STEP && nextIdx < meta.length; i++, nextIdx++)
      mountPage(meta[nextIdx]);
    trimWindow();
    updateProgress();
  }

  function trimWindow() {
    // keep at most MAX_MOUNTED around the scroll position
    const viewTop = scroller.scrollTop;
    while (mounted.length > MAX_MOUNTED) {
      const first = mounted[0], last = mounted[mounted.length - 1];
      const dropFirst = Math.abs(first.el.offsetTop - viewTop)
                      > Math.abs(last.el.offsetTop + last.el.offsetHeight - viewTop);
      const victim = dropFirst ? mounted.shift() : mounted.pop();
      const m = meta.find(x => x.n === victim.n);
      if (m) victim.el.replaceWith(Object.assign(ph(m), {}));
      else victim.el.remove();
    }
  }

  function updateProgress() {
    const cur = Math.min(meta.length, Math.max(1, mounted.length ? mounted[Math.min(mounted.length - 1, Math.floor(mounted.length / 2))].n : 1));
    const el = scroller.querySelector("#reader-progress");
    if (el) el.textContent = `${cur} / ${meta.length}`;
  }

  // initial chunk (1..FIRST_CHUNK)
  for (let i = 0; i < FIRST_CHUNK && nextIdx < meta.length; i++, nextIdx++)
    mountPage(meta[nextIdx]);
  // preload 2 ahead
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
    trimWindow(); updateProgress();
  }, { passive: true });

  const onKey = (e) => { if (e.key === "Escape") closeReader(); };
  document.addEventListener("keydown", onKey, { once: true });
  return true;
}

export function closeReader() {
  if (_overlay) { _overlay.remove(); _overlay = null; }
  document.body.style.overflow = "";
}
