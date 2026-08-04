/*
  pages/bookmarks.js — User's saved galleries

  Reads from /api/bookmarks and renders the same card grid as search.
  Uses the shared detail-sheet plugin so tapping a bookmark shows the
  full nhentai-style detail view.
*/

import { api } from "core/api.js";
import { make, h } from "core/components.js";
import { store } from "core/state.js";
import { openGalleryDetail } from "plugins/detail-sheet.js";

export async function render(root, { me }) {
  const $grid = h("div", { class: "card-grid" });
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
        h("div", { class: "title" }, "No bookmarks yet"),
        h("div", {}, "Tap ⭐ on any gallery to save it here."),
      ));
      return;
    }
    for (const g of items) {
      $grid.appendChild(make("card", {
        id: g.id,
        title: g.title,
        cover: g.cover,
        pages: g.pages,
        badge: g.pages ? `${g.pages}p` : null,
        onOpen: () => openGalleryDetail(g, me),
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
