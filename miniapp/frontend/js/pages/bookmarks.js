/*
  pages/bookmarks.js — User's saved galleries

  Reads from /api/bookmarks and renders the same card grid as search.
  Actions on tap re-use plugins/card-actions.js (so "Queue" still works from
  the bookmark view without duplicating logic).
*/

import { api } from "core/api.js";
import { make, h } from "core/components.js";
import { store } from "core/state.js";
import { cardActions } from "plugins/card-actions.js";
// v12.3: prefetchGallery import REMOVED (same reason as search.js — the
// background detail-warming storm was the #1 cause of the 429 flood).

export async function render(root, { me }) {
  // v11.8 (#6b): compact 3-per-row grid on mobile, 4-per-row on ≥720px.
  const $grid = h("div", { class: "card-grid card-grid-compact" });
  root.appendChild($grid);
  $grid.appendChild(make("skeleton", { variant: "card-grid", count: 4 }));

  try {
    const rows = await api.get("/api/bookmarks");
    const items = rows.items || [];
    store.set("bookmarks", items);
    $grid.innerHTML = "";
    if (items.length === 0) {
      $grid.appendChild(h("div", { class: "empty", style: { gridColumn: "1 / -1" } },
        h("div", { class: "icon" }, "⭐"),
        h("div", { class: "title" }, "Nothing saved yet"),   // v11.8 (#6a)
        h("div", {}, "Tap ⭐ on any gallery to save it here."),
      ));
      return;
    }
    for (const g of items) {
      // v12.3: prefetch removed — card taps open the minimal sheet which
      // fetches detail on demand, not on paint.
      $grid.appendChild(make("card", {
        id: g.id, title: g.title, cover: g.cover, pages: g.pages,
        badge: null,  // v11.9 (#5): removed "Np" badge
        is_cached: typeof g.is_cached === "boolean" ? g.is_cached : false,
        onOpen: () => openDetail(g, me),
      }));
    }
  } catch (e) {
    $grid.innerHTML = "";
    $grid.appendChild(h("div", { class: "empty" },
      h("div", { class: "icon" }, "⚠️"),
      h("div", { class: "title" }, "Failed to load bookmarks"),
      h("div", {}, String(e.message || e)),
    ));
  }
}

function openDetail(g, me) {
  // Fetch V2 dedup status once, in the background — sheet appears
  // immediately, primary-button label updates once the RTT lands.
  if (!g.v2_status) {
    api.get(`/api/gallery/${g.id}/status`)
      .then(s => { g.v2_status = s || { known: false }; })
      .catch(() => { g.v2_status = { known: false }; });
  }

  // Unwrap function-valued label / icon / disabled from V2 dynamic actions.
  const _val = (v, ctx) => (typeof v === "function" ? v(ctx) : v);
  const actions = cardActions
    .filter(a => !a.when || a.when({ gallery: g, me }))
    .map(a => {
      const ctx = { gallery: g, me };
      return {
        label:    `${_val(a.icon, ctx)} ${_val(a.label, ctx)}`,
        kind:     a.kind || "secondary",
        disabled: _val(a.disabled, ctx) || false,
        onClick:  (sheetApi) => a.run({ gallery: g, me, close: sheetApi.close }),
      };
    });
  const body = h("div", { style: { textAlign: "center" } },
    g.cover ? h("img", { src: g.cover, alt: g.title,
      style: { width: "60%", maxWidth: "220px", borderRadius: "12px",
               margin: "0 auto", display: "block" } }) : null,
    h("div", { style: { marginTop: "12px", fontWeight: "600" } }, g.title || `#${g.id}`),
  );
  make("sheet", { title: "Saved", body, actions }).open();  // v11.8 (#6a)
}
