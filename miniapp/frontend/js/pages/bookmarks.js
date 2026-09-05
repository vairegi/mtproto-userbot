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
        is_cached: typeof g.is_cached === "boolean" ? g.is_cached : undefined,
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
  // v12.55: shared rich sheet — identical UI/updates as every other page.
  if (!g.v2_status) {
    api.get(`/api/gallery/${g.id}/status`)
      .then(st => { g.v2_status = st || { known: false }; })
      .catch(() => { g.v2_status = { known: false }; });
  }
  import("plugins/detail-sheet.js?v=12.62").then(m => m.openGalleryDetail(g, me));
}
