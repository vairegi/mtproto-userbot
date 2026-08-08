/*
  plugins/star-rating.js — v11.7

  Interactive 1..5-star widget for a gallery detail sheet. Renders once,
  fetches the aggregate + caller's own vote from /api/ratings/{gid}, and
  wires click/hover so tapping a star POSTs the vote and updates the row.

  Usage:
    import { renderStarRating } from "plugins/star-rating.js";
    root.appendChild(renderStarRating(gallery.id));
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { haptic } from "core/telegram.js";

const STAR_FILLED = "★";
const STAR_HOLLOW = "☆";

export function renderStarRating(galleryId) {
  const wrap = h("div", { class: "star-rating", style: {
    display: "flex", alignItems: "center", gap: "10px",
    padding: "10px 12px", margin: "10px 0",
    background: "var(--du-bg-2)",
    border: "1px solid var(--du-border)",
    borderRadius: "10px",
    flexWrap: "wrap",
  }});

  const label = h("span", { style: {
    fontSize: "12px", color: "var(--du-ink-mid)", minWidth: "56px",
  }}, "Your rating:");

  const starsRow = h("div", { style: {
    display: "inline-flex", gap: "2px", cursor: "pointer",
    fontSize: "24px", lineHeight: "1", userSelect: "none",
  }});

  const avgLabel = h("span", { style: {
    fontSize: "12px", color: "var(--du-ink-lo)", marginLeft: "auto",
  }}, "—");

  let myStars = 0;   // 0 = no vote
  let hover   = 0;

  function paint() {
    starsRow.innerHTML = "";
    const shown = hover || myStars;
    for (let s = 1; s <= 5; s++) {
      const on = s <= shown;
      const dim = (!on && myStars === 0);
      starsRow.appendChild(h("span", {
        "data-value": String(s),
        style: {
          color: on ? "var(--du-accent)" : (dim ? "var(--du-ink-lo)" : "var(--du-ink-mid)"),
          transition: "color 120ms ease, transform 100ms ease",
          transform: hover === s ? "scale(1.18)" : "scale(1)",
        },
      }, on ? STAR_FILLED : STAR_HOLLOW));
    }
  }

  starsRow.addEventListener("mousemove", (e) => {
    const t = e.target.closest("[data-value]");
    hover = t ? parseInt(t.dataset.value, 10) : 0;
    paint();
  });
  starsRow.addEventListener("mouseleave", () => { hover = 0; paint(); });

  starsRow.addEventListener("click", async (e) => {
    const t = e.target.closest("[data-value]");
    if (!t) return;
    const n = parseInt(t.dataset.value, 10);
    if (!(n >= 1 && n <= 5)) return;
    haptic("medium");
    const prev = myStars;
    myStars = n;
    paint();
    try {
      const r = await api.post(`/api/ratings/${encodeURIComponent(galleryId)}`,
                               { stars: n });
      if (r && r.avg != null && r.count != null) {
        avgLabel.textContent = `Avg ${r.avg.toFixed(2)} · ${r.count} vote${r.count === 1 ? "" : "s"}`;
      }
      clearBtn.style.display = "inline-block";
      try { make("toast", { text: `Rated ${n}★`, kind: "success" }); } catch (_) {}
    } catch (err) {
      myStars = prev;
      paint();
      try { make("toast", { text: "Rating failed: " + (err.message || err), kind: "error" }); } catch (_) {}
    }
  });

  const clearBtn = h("button", {
    style: {
      background: "transparent", border: "0",
      color: "var(--du-ink-lo)", cursor: "pointer",
      fontSize: "12px", padding: "0 4px", display: "none",
    },
    title: "Remove your rating",
    onclick: async (e) => {
      e.preventDefault();
      haptic("light");
      try {
        const r = await api.del(`/api/ratings/${encodeURIComponent(galleryId)}`);
        myStars = 0; paint();
        clearBtn.style.display = "none";
        if (r && r.avg != null) {
          avgLabel.textContent = r.count
            ? `Avg ${r.avg.toFixed(2)} · ${r.count} vote${r.count === 1 ? "" : "s"}`
            : "No votes yet";
        }
      } catch (err) {
        try { make("toast", { text: "Clear failed: " + (err.message || err), kind: "error" }); } catch (_) {}
      }
    },
  }, "× clear");

  paint();
  wrap.append(label, starsRow, clearBtn, avgLabel);

  (async () => {
    try {
      const r = await api.get(`/api/ratings/${encodeURIComponent(galleryId)}`);
      if (r) {
        myStars = r.my_stars || 0;
        paint();
        clearBtn.style.display = myStars ? "inline-block" : "none";
        avgLabel.textContent = r.count
          ? `Avg ${r.avg.toFixed(2)} · ${r.count} vote${r.count === 1 ? "" : "s"}`
          : "No votes yet";
      }
    } catch (_) { /* endpoint down; leave placeholder */ }
  })();

  return wrap;
}
