console.info('[detail-sheet] build=v12.57');
/*
  detail-sheet.js — Rich gallery detail sheet

  Opens immediately with cover + name (from the grid row), then enriches
  with /api/gallery/{id} (grouped tags, favorites, upload date).

  Used by pages/search.js and pages/bookmarks.js. Action buttons come from
  plugins/card-actions.js — to change buttons, edit THAT file, not this one.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { cardActions, warmSaveCount } from "plugins/card-actions.js";
import { renderStarRating } from "plugins/star-rating.js";  // v11.7

const GROUP_LABELS = {
  parody:    "Parodies",
  character: "Characters",
  tag:       "Tags",
  artist:    "Artists",
  group:     "Groups",
  language:  "Languages",
  category:  "Categories",
};
const GROUP_ORDER = ["parody", "character", "tag", "artist", "group",
                     "language", "category"];

/* v11.8 (#2): detail prefetch cache. search.js warms this as cards render,
   so opening a detail sheet feels instant — the enrich() path reads from
   the cache first and refreshes in the background.

   v12.1 (A): the render log showed a 429 STORM — 25 back-to-back
   /api/gallery/<id> hits from prefetching a single grid page. Now:
     - MAX_INFLIGHT = 2 concurrent prefetches; extra requests queue.
     - Any 503 (upstream rate-limited) trips a 60s CIRCUIT BREAKER: the
       queue is dropped and NEW prefetch requests are silently ignored
       until the breaker resets. User-initiated openGalleryDetail() still
       goes through unconditionally — only opportunistic prefetches pause.
     - Retry-After from the 503 response tunes the breaker duration. */
const _detailCache = new Map();          // gid -> detail dict
const _detailInflight = new Map();       // gid -> Promise
const _DETAIL_CACHE_MAX = 60;            // LRU-ish cap
const _PREFETCH_MAX_INFLIGHT = 2;
const _prefetchQueue = [];               // [{ key, resolve }]
let   _prefetchActive = 0;
let   _circuitOpenUntil = 0;             // epoch ms; 0 = closed

function _circuitOpen() { return Date.now() < _circuitOpenUntil; }

function _tripCircuit(retryAfterSec) {
  const secs = Number(retryAfterSec) > 0 ? Number(retryAfterSec) : 60;
  _circuitOpenUntil = Date.now() + secs * 1000;
  // Drop everything queued — they'll be re-prefetched next time the grid
  // paints. Silent by design so we don't spam the console.
  while (_prefetchQueue.length) {
    const q = _prefetchQueue.shift();
    q.resolve(null);
  }
}

function _drainQueue() {
  while (_prefetchActive < _PREFETCH_MAX_INFLIGHT && _prefetchQueue.length) {
    if (_circuitOpen()) {
      // Breaker tripped mid-drain; drop the rest.
      while (_prefetchQueue.length) _prefetchQueue.shift().resolve(null);
      return;
    }
    const { key, resolve } = _prefetchQueue.shift();
    if (_detailCache.has(key)) { resolve(_detailCache.get(key)); continue; }
    _prefetchActive += 1;
    api.get(`/api/gallery/${encodeURIComponent(key)}`)
      .then(d => {
        if (d && d.id) {
          if (_detailCache.size >= _DETAIL_CACHE_MAX) {
            _detailCache.delete(_detailCache.keys().next().value);
          }
          _detailCache.set(key, d);
        }
        resolve(d);
      })
      .catch(err => {
        // api.get surfaces the HTTP status on err.status when available.
        const status = err && (err.status || err.code);
        if (status === 503 || status === 429) {
          const retryAfter = err && (err.retry_after || err.retryAfter);
          _tripCircuit(retryAfter || 60);
        }
        resolve(null);
      })
      .finally(() => {
        _prefetchActive -= 1;
        _detailInflight.delete(key);
        // Yield a microtask so we don't recurse hot.
        Promise.resolve().then(_drainQueue);
      });
  }
}

export function prefetchGallery(gid) {
  if (!gid) return null;
  const key = String(gid);
  if (_detailCache.has(key)) return Promise.resolve(_detailCache.get(key));
  if (_detailInflight.has(key)) return _detailInflight.get(key);
  // v12.1: skip opportunistic prefetch entirely when the breaker is open.
  if (_circuitOpen()) return null;
  const p = new Promise(resolve => {
    _prefetchQueue.push({ key, resolve });
  });
  _detailInflight.set(key, p);
  _drainQueue();
  return p;
}

// v12.1: expose for tests / diagnostics.
export function _prefetchStats() {
  return {
    active:  _prefetchActive,
    queued:  _prefetchQueue.length,
    breaker: _circuitOpen() ? Math.ceil((_circuitOpenUntil - Date.now()) / 1000) : 0,
    cached:  _detailCache.size,
  };
}

export function openGalleryDetail(g, me) {
  const body = h("div", { class: "d-root" });

  // Improvement (UI text v9): action.label / action.icon may be a string
  // OR a function of ctx (used by the dynamic queue / bookmark labels).
  // The old code interpolated them as template literals — a function
  // would have been stringified to its source text. Resolve them
  // properly here.
  const resolve = (val, ctx) =>
    typeof val === "function" ? val(ctx) : val;

  const sheet = make("sheet", {
    title: "Gallery",
    body,
    actions: cardActions
      .filter(a => !a.when || a.when({ gallery: g, me }))
      .map(a => {
        const ctx = { gallery: g, me };
        const icon = resolve(a.icon, ctx);
        const label = resolve(a.label, ctx);
        const isDisabled = typeof a.disabled === "function"
          ? a.disabled(ctx)
          : !!a.disabled;
        return {
          label: `${icon ? icon + " " : ""}${label}`,
          kind: a.kind || "secondary",
          disabled: isDisabled,
          onClick: (s) => a.run({ gallery: g, me, close: s.close }),
        };
      }),
  });

  renderBase(body, g);
  sheet.open();
  // v12.57: pass the sheet root so enrich→paintFull can mount the
  // "Similar to this" section AFTER the actions bar (Download/Save/Share),
  // not inside the scroll body.
  enrich(body, g, sheet.el);
  // v11.8 (#5): pre-fetch the global save count so the Save button label
  // shows "Save · 312" on the next render. Fire-and-forget.
  try { warmSaveCount(g.id); } catch (_) {}
  return sheet;
}

function renderBase(root, g) {
  root.innerHTML = "";
  root.append(
    g.cover
      ? h("img", { class: "d-cover", src: g.cover, alt: g.title || "cover" })
      : h("div", { class: "d-cover skeleton" }),
    h("div", { class: "d-title" }, g.title || `#${g.id}`),
    h("div", { class: "d-sub" }, `#${g.id}` + (g.pages ? ` · ${g.pages} pages` : "")),
    h("div", { class: "d-loading" }, "Loading details…"),
  );
}

async function enrich(root, g, sheetEl) {
  const key = String(g.id || "");
  // v11.9 (#3): ALWAYS kick off a prefetch — even if the card wasn't in
  // the viewport yet. This makes "tapped → details appear" the default.
  if (!_detailCache.has(key) && !_detailInflight.has(key)) {
    prefetchGallery(key);
  }

  // v11.9 (#3): instant render from the prefetch cache when available.
  let d = _detailCache.get(key) || null;
  if (d) {
    paintFull(root, d, sheetEl);
  } else {
    // No cache — swap the "Loading details…" line for a skeleton block so
    // the sheet doesn't feel frozen. We still refresh in the background.
    const l = root.querySelector(".d-loading");
    if (l) l.textContent = "Loading…";
  }

  // Wait for the in-flight prefetch (or fetch fresh) — whichever completes
  // first. This avoids double-fetching the same gallery.
  // v11.9: on a 503 (upstream rate-limited), wait the Retry-After window
  // (capped at 65s) and retry ONCE — that's what turns the old
  // "Loading details… forever" failure into a slow-but-working load.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const inflight = _detailInflight.get(key);
      const fresh = inflight ? await inflight
                             : await api.get(`/api/gallery/${g.id}`);
      if (fresh && fresh.id) {
        if (_detailCache.size >= _DETAIL_CACHE_MAX) {
          _detailCache.delete(_detailCache.keys().next().value);
        }
        _detailCache.set(key, fresh);
        d = fresh;
        if (root.isConnected) paintFull(root, d, sheetEl);
        return;
      }
    } catch (e) {
      const status = e && (e.status || (e.response && e.response.status));
      if (status === 503 && attempt === 0) {
        const ra = parseInt(
          (e.headers && (e.headers.get ? e.headers.get("Retry-After") : e.headers["Retry-After"]))
          || "10", 10);
        const waitMs = Math.min(65000, Math.max(2000, (isNaN(ra) ? 10 : ra) * 1000));
        const l = root.querySelector(".d-loading");
        if (l) l.textContent = "Server busy — retrying…";
        await new Promise(r => setTimeout(r, waitMs));
        if (!root.isConnected) return;
        continue;  // second (final) attempt
      }
      break;
    }
    break;
  }

  // If we got here and there's still nothing cached, hide the loader so
  // the user isn't staring at it forever.
  if (!d) {
    const l = root.querySelector(".d-loading");
    if (l) l.remove();
    return;
  }
  if (root.isConnected) paintFull(root, d, sheetEl);
}

/* v11.8 (#2 + #3): single paint function — the full detail body is rebuilt
   from scratch each time, so cache-hit renders and background refreshes
   share one code path. All metadata (incl. Uploaded) lives in ONE unified
   card per #3. */
function paintFull(root, d, sheetEl) {
  console.info('[similar-to-this] paintFull enter, gid=', d && d.id);
  root.innerHTML = "";
  try {
    } catch (e) {
    // v12.54: a stale star-rating plugin must never abort the detail body.
    console.warn('[detail-sheet] renderStarRating failed (non-fatal):', e);
  }
  root.append(
    d.cover
      ? h("img", { class: "d-cover", src: d.cover, alt: d.title })
      : h("div", { class: "d-cover skeleton" }),

    // Pretty title — BOLD only.
    h("div", { class: "d-title" }, d.title || `#${d.id}`),

    // Full english + japanese titles underneath (smaller, not bold).
    d.title_english
      ? h("div", { class: "d-full-title" }, d.title_english) : null,
    d.title_japanese
      ? h("div", { class: "d-jpn-title" }, d.title_japanese) : null,

    h("div", { class: "d-sub" },
      `#${d.id}`
      + (d.pages ? ` · ${d.pages} pages` : "")
      + (d.favorites != null ? ` · ♥ ${fmtNum(d.favorites)}` : "")
      + (d.upload_date ? ` · 📅 ${fmtDate(d.upload_date)}` : "")),
  );

  // v11.7: interactive star-rating widget between header and metadata card.
  root.appendChild(renderStarRating(d.id));

  /* v11.8 (#3): ONE unified metadata card. Parodies/Characters/Artists/
     Groups/Languages/Categories/Tags/Uploaded all stack inside a single
     container with faint dividers — no more floating per-category boxes. */
  const groups = d.groups || {};
  const card = h("div", {
    class: "d-meta-card",
    style: {
      background: "var(--du-bg-1)",
      border: "1px solid var(--du-border)",
      borderRadius: "12px",
      padding: "4px 12px",
      marginTop: "10px",
    },
  });
  let firstRow = true;
  const addRow = (labelText, valueNode) => {
    const rowStyle = {
      display: "flex", gap: "10px", alignItems: "baseline",
      padding: "8px 0",
      flexWrap: "wrap",
    };
    if (!firstRow) {
      rowStyle.borderTop = "1px solid var(--du-divider, rgba(255,255,255,0.06))";
    }
    firstRow = false;
    card.appendChild(h("div", { class: "d-meta-row", style: rowStyle },
      h("span", {
        class: "d-meta-label",
        style: {
          fontSize: "11px", fontWeight: "700", minWidth: "86px",
          color: "var(--du-ink-lo)", textTransform: "uppercase",
          letterSpacing: "0.4px", flexShrink: "0",
        },
      }, labelText),
      valueNode,
    ));
  };

  for (const key of GROUP_ORDER) {
    const arr = groups[key];
    if (!arr || !arr.length) continue;
    addRow((GROUP_LABELS[key] || key),
      h("span", { class: "d-meta-tags", style: {
        display: "flex", flexWrap: "wrap", gap: "4px", flex: "1",
      }},
        ...arr.slice(0, 12).map(t =>
          h("span", { class: "d-tag" },
            t.name,
            t.count ? h("span", { class: "cnt" }, " " + fmtNum(t.count)) : null))),
    );
  }
  // v11.8 (#2): Uploaded row — matches nhentai's "Uploaded: <date>" line.
  if (d.upload_date) {
    addRow("Uploaded",
      h("span", { style: { fontSize: "13px", color: "var(--du-ink-mid)" } },
        fmtDate(d.upload_date)),
    );
  }
  // v11.9 (#4): Saves row — live count from /api/bookmarks/count/{id}.
  // Renders "…" immediately, then fills in when the count arrives.
  const savesVal = h("span", {
    style: { fontSize: "13px", color: "var(--du-ink-mid)", fontWeight: "600" },
  }, "…");
  addRow("Saves", savesVal);
  (async () => {
    try {
      const r = await api.get(`/api/bookmarks/count/${encodeURIComponent(d.id)}`);
      const n = Number(r && r.saves) || 0;
      savesVal.textContent = (n >= 1000)
        ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k"
        : String(n);
    } catch (_) { savesVal.textContent = "0"; }
  })();
  if (!firstRow) root.appendChild(card);   // only render when it has content

  // v12.57: "Similar to this" is now anchored to the SHEET root (sheetEl),
  // not the scroll body. That places it visually BELOW the actions bar
  // (Download / Save / Share), which is what the user asked for.
  // Fallback to body only if sheetEl is unavailable (defensive).
  mountSimilarRow(sheetEl || root, d.id);
}

function mountSimilarRow(host, gid) {
  console.info('[similar-to-this] mountSimilarRow v12.57 called, gid=', gid, 'host?', !!host);
  if (!gid || !host) { console.warn('[similar-to-this] skipped — no gid or host'); return; }

  // v12.57: guard against re-mount when paintFull runs twice (cache + fresh).
  const prev = host.querySelector(":scope > .d-similar");
  if (prev) prev.remove();

  const section = h("div", {
    class: "d-similar",
    style: {
      // Sits directly under the actions bar; bold divider, comfortable pad.
      marginTop: "14px",
      paddingTop: "14px",
      paddingBottom: "4px",
      borderTop: "2px solid var(--du-divider, rgba(255,255,255,0.14))",
      display: "none",
    },
  });

  // Big, unambiguous section header — brighter than the metadata labels,
  // with a small "swipe" hint on the right so the affordance is obvious.
  const heading = h("div", {
    class: "d-similar-heading",
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      gap: "8px",
      marginBottom: "10px",
      color: "var(--du-ink, #fff)",
      fontSize: "14px",
      fontWeight: "800",
      letterSpacing: "0.6px",
      textTransform: "uppercase",
    },
  },
    h("span", {}, "SIMILAR TO THIS"),
    h("span", {
      style: {
        fontSize: "11px", fontWeight: "600",
        color: "var(--du-ink-lo, #9aa)",
        textTransform: "none",
        letterSpacing: "0.2px",
        opacity: "0.9",
      },
    }, "swipe →"),
  );

  const strip = h("div", {
    class: "d-similar-strip",
    style: {
      display: "grid",
      gridAutoFlow: "column",
      gridAutoColumns: "minmax(112px, 34vw)",
      gap: "10px",
      overflowX: "auto",
      overflowY: "hidden",
      // v12.57: NO scroll-snap. Snap on Android WebViews inside a
      // fixed bottom-sheet is the primary "stuck" culprit. Free scroll wins.
      WebkitOverflowScrolling: "touch",
      touchAction: "pan-x",
      overscrollBehaviorX: "contain",
      scrollbarWidth: "none",
      msOverflowStyle: "none",
      paddingBottom: "6px",
    },
  });

  // Positioned wrapper so we can layer a right-edge fade on top of the
  // strip (the "there's more" affordance) without breaking scroll.
  const stripWrap = h("div", {
    class: "d-similar-stripwrap",
    style: {
      position: "relative",
      contain: "content",   // isolate touch region from parent scroll
      touchAction: "pan-x",
    },
  },
    strip,
    h("div", {
      class: "d-similar-fade",
      style: {
        position: "absolute",
        top: "0", right: "0", bottom: "6px",
        width: "28px",
        pointerEvents: "none",
        background: "linear-gradient(to left, rgba(0,0,0,0.55), transparent)",
      },
    }),
  );

  section.append(heading, stripWrap);
  host.appendChild(section);   // v12.57: appended to sheet.el, AFTER actions

  (async () => {
    let items = [];
    try {
      console.log("[similar] fetching suggestions for", gid);
      const r = await api.get(`/api/gallery/${encodeURIComponent(gid)}/suggestions?limit=8`);
      items = (r && Array.isArray(r.items)) ? r.items : [];
    } catch (e) {
      console.warn("[similar] fetch failed for", gid, e);
      items = [];
    }
    console.log("[similar]", gid, "->", items.length, "items");
    if (!items.length) return;

    for (const s of items) {
      if (!s || !s.id) continue;
      const card = h("div", {
        class: "d-similar-card",
        style: {
          borderRadius: "10px",
          overflow: "hidden",
          background: "var(--du-bg-1, #1b1b1b)",
          border: "1px solid var(--du-border, rgba(255,255,255,0.08))",
          cursor: "pointer",
          display: "flex",
          flexDirection: "column",
          position: "relative",
        },
      });
      const cover = s.cover
        ? h("img", {
            class: "d-similar-cover",
            src: s.cover,
            alt: s.title || `#${s.id}`,
            style: {
              width: "100%",
              aspectRatio: "3 / 4",
              objectFit: "cover",
              display: "block",
            },
            loading: "lazy",
          })
        : h("div", {
            class: "d-similar-cover skeleton",
            style: { width: "100%", aspectRatio: "3 / 4" },
          });
      if (s.is_cached === true || s.is_cached === false) {
        const pill = h("div", {
          class: "status-pill" + (s.is_cached ? "" : " pill-queue"),
          style: {
            position: "absolute", top: "6px", right: "6px",
            fontSize: "11px", padding: "2px 6px", borderRadius: "8px",
            background: "rgba(0,0,0,0.55)", zIndex: "2",
          },
        }, s.is_cached ? "⚡⚡" : "📥");
        card.appendChild(pill);
      }
      const title = h("div", {
        class: "d-similar-title",
        style: {
          fontSize: "11px",
          lineHeight: "1.25",
          padding: "6px 8px",
          color: "var(--du-ink-mid)",
          display: "-webkit-box",
          WebkitLineClamp: "2",
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        },
      }, s.title_en_clean || s.title || `#${s.id}`);
      card.append(cover, title);

      card.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        try { openGalleryDetail(s); } catch (_) { /* no-op */ }
      });
      strip.appendChild(card);
    }

    section.style.display = "block";
  })();
}

function fmtNum(n) {
  n = Number(n) || 0;
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k";
  return String(n);
}

function fmtDate(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric" });
  } catch (_) { return ""; }
}
