/*
  plugins/home-rows.js — v11.7

  Two home-tab widgets that render ABOVE the search grid when the user
  has no active query:

    * renderTrendingTags(onPick)  → horizontal chip row of top tags from
                                    /api/trending/tags. Tapping a chip
                                    calls onPick("tag:<name>") so search.js
                                    can rerun the query with the tag filter.

    * renderRecommendations(me)  → "Because you saved …" card grid from
                                    /api/recommendations. Rendered only when
                                    the caller has any bookmarks to seed
                                    from; hidden otherwise.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { haptic } from "core/telegram.js";
import { openGalleryDetail } from "plugins/detail-sheet.js?v=12.56";
// v12.3: prefetchGallery import removed — no more background warming storm.

/* ---- Trending Tags ------------------------------------------------- */
export function renderTrendingTags(onPick) {
  const wrap = h("div", {
    class: "home-trending-tags",
    style: { display: "none", margin: "8px 0 12px" },
  });
  const label = h("div", {
    style: {
      fontSize: "12px", color: "var(--du-ink-lo)",
      fontWeight: "600", margin: "0 0 6px",
      textTransform: "uppercase", letterSpacing: "0.4px",
    },
  }, "🔥 Trending this week");
  const row = h("div", {
    style: {
      display: "flex", flexWrap: "nowrap", gap: "6px",
      overflowX: "auto", padding: "2px",
      scrollbarWidth: "none",
    },
  });
  wrap.append(label, row);

  (async () => {
    let items = [];
    try {
      const r = await api.get("/api/trending/tags?days=7&limit=15");
      items = r.items || [];
    } catch (_) { items = []; }
    if (!items.length) return;
    wrap.style.display = "block";
    for (const t of items) {
      const chip = h("button", {
        class: "chip",
        style: {
          flex: "0 0 auto",
          padding: "6px 10px",
          borderRadius: "999px",
          background: "var(--du-bg-2)",
          border: "1px solid var(--du-border)",
          color: "var(--du-ink-hi)",
          fontSize: "12px",
          cursor: "pointer",
          transition: "background 160ms ease, border-color 160ms ease",
        },
      },
        h("span", {}, t.name),
        h("span", {
          style: { color: "var(--du-ink-lo)", marginLeft: "6px",
                   fontSize: "10px" }
        }, "×" + t.count),
      );
      chip.addEventListener("mouseenter", () => {
        chip.style.borderColor = "var(--du-accent)";
      });
      chip.addEventListener("mouseleave", () => {
        chip.style.borderColor = "var(--du-border)";
      });
      chip.addEventListener("click", () => {
        haptic("light");
        onPick && onPick(`tag:${t.name}`);
      });
      row.appendChild(chip);
    }
  })();

  return wrap;
}


/* ---- Recommendations ("Because you saved …") ----------------------- */
export function renderRecommendations(me) {
  const wrap = h("div", {
    class: "home-recommendations",
    style: { display: "none", margin: "10px 0 18px" },
  });
  const header = h("div", { style: { margin: "0 0 8px" }},
    h("div", {
      style: {
        fontSize: "12px", color: "var(--du-ink-lo)",
        fontWeight: "600", textTransform: "uppercase",
        letterSpacing: "0.4px",
      },
    }, "✨ For you"),
    h("div", {
      class: "recs-subtitle",
      style: { fontSize: "11px", color: "var(--du-ink-lo)",
               marginTop: "2px" },
    }, ""),
  );
  const grid = h("div", { class: "card-grid" });
  wrap.append(header, grid);

  (async () => {
    let payload = null;
    try {
      payload = await api.get("/api/recommendations?limit=8");
    } catch (_) { payload = null; }
    if (!payload || !payload.has_seed || !(payload.items || []).length) {
      return;
    }
    wrap.style.display = "block";
    const sub = header.querySelector(".recs-subtitle");
    if (sub && payload.seed_tags && payload.seed_tags.length) {
      sub.textContent = "Because you like: " + payload.seed_tags.join(", ");
    }
    for (const g of payload.items) {
      // v12.3: prefetch removed — sheet fetches detail on tap.
      const card = make("card", {
        id: g.id, title: g.title, cover: g.cover, pages: g.pages,
        badge: null,  // v11.9 (#5): removed "Np" badge
        is_cached: typeof g.is_cached === "boolean" ? g.is_cached : undefined,
        onOpen: () => openGalleryDetail(g, me),
      });
      grid.appendChild(card);
    }
  })();

  return wrap;
}
