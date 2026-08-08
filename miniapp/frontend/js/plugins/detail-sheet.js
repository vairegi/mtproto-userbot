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
   the cache first and refreshes in the background. */
const _detailCache = new Map();          // gid -> detail dict
const _detailInflight = new Map();       // gid -> Promise
const _DETAIL_CACHE_MAX = 60;            // LRU-ish cap

export function prefetchGallery(gid) {
  if (!gid) return;
  const key = String(gid);
  if (_detailCache.has(key) || _detailInflight.has(key)) return;
  const p = api.get(`/api/gallery/${encodeURIComponent(key)}`)
    .then(d => {
      _detailInflight.delete(key);
      if (d && d.id) {
        if (_detailCache.size >= _DETAIL_CACHE_MAX) {
          // Evict oldest key (Map preserves insertion order).
          _detailCache.delete(_detailCache.keys().next().value);
        }
        _detailCache.set(key, d);
      }
      return d;
    })
    .catch(() => { _detailInflight.delete(key); return null; });
  _detailInflight.set(key, p);
  return p;
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
  enrich(body, g);
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

async function enrich(root, g) {
  const key = String(g.id || "");
  // v11.8 (#2): instant render from the prefetch cache when available.
  let d = _detailCache.get(key) || null;
  if (d) {
    paintFull(root, d);
  }
  // Always refresh in the background (fresh tags / ratings / upload date).
  try {
    const inflight = _detailInflight.get(key);
    const fresh = inflight ? await inflight
                           : await api.get(`/api/gallery/${g.id}`);
    if (fresh && fresh.id) {
      _detailCache.set(key, fresh);
      d = fresh;
      // Only re-paint if the user hasn't navigated away (root still mounted).
      if (root.isConnected) paintFull(root, d);
    } else if (!d) {
      const l = root.querySelector(".d-loading");
      if (l) l.remove();
      return;
    }
  } catch (_) {
    const l = root.querySelector(".d-loading");
    if (l) l.remove();
    if (!d) return;
  }
  if (!d || !d.id) return;

  paintFull(root, d);
}

/* v11.8 (#2 + #3): single paint function — the full detail body is rebuilt
   from scratch each time, so cache-hit renders and background refreshes
   share one code path. All metadata (incl. Uploaded) lives in ONE unified
   card per #3. */
function paintFull(root, d) {
  root.innerHTML = "";
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
  if (!firstRow) root.appendChild(card);   // only render when it has content
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
