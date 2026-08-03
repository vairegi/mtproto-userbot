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
        id: g.id, title: g.title, cover: g.cover, pages: g.pages,
        badge: g.pages ? `${g.pages}p` : null,
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
  const actions = cardActions
    .filter(a => !a.when || a.when({ gallery: g, me }))
    .map(a => ({
      label: `${a.icon} ${a.label}`,
      kind: a.kind || "secondary",
      onClick: (sheetApi) => a.run({ gallery: g, me, close: sheetApi.close }),
    }));
  const body = h("div", { style: { textAlign: "center" } },
    g.cover ? h("img", { src: g.cover, alt: g.title,
      style: { width: "60%", maxWidth: "220px", borderRadius: "12px",
               margin: "0 auto", display: "block" } }) : null,
    h("div", { style: { marginTop: "12px", fontWeight: "600" } }, g.title || `#${g.id}`),
  );
  make("sheet", { title: "Bookmark", body, actions }).open();
}
