/*
  detail-sheet.js — Rich gallery detail sheet

  Opens immediately with cover + name (from the grid row), then enriches
  with /api/gallery/{id} (grouped tags, favorites, upload date, etc).

  Used by pages/search.js and pages/bookmarks.js. Action buttons come from
  plugins/card-actions.js — to change buttons, edit THAT file, not this one.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { cardActions } from "plugins/card-actions.js";

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

export function openGalleryDetail(g, me) {
  const body = h("div", { class: "d-root" });

  const sheet = make("sheet", {
    title: "Gallery",
    body,
    actions: cardActions
      .filter(a => !a.when || a.when({ gallery: g, me }))
      .map(a => ({
        label: `${a.icon} ${a.label}`,
        kind: a.kind || "secondary",
        onClick: (s) => a.run({ gallery: g, me, close: s.close }),
      })),
  });

  renderBase(body, g);
  sheet.open();
  enrich(body, g);
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
  let d;
  try {
    d = await api.get(`/api/gallery/${g.id}`);
  } catch (_) {
    const l = root.querySelector(".d-loading");
    if (l) l.remove();
    return;                                     // keep the basic view
  }
  if (!d || !d.id) {
    const l = root.querySelector(".d-loading");
    if (l) l.remove();
    return;
  }

  root.innerHTML = "";
  root.append(
    d.cover
      ? h("img", { class: "d-cover", src: d.cover, alt: d.title })
      : h("div", { class: "d-cover skeleton" }),

    // Pretty title — BOLD only, per spec.
    h("div", { class: "d-title" }, d.title || `#${d.id}`),

    // Full titles underneath (not bold, smaller).
    d.title_english
      ? h("div", { class: "d-full-title" }, d.title_english) : null,
    d.title_japanese
      ? h("div", { class: "d-jpn-title" }, d.title_japanese) : null,

    // Meta line: id · pages · favorites · uploaded
    h("div", { class: "d-sub" },
      `#${d.id}`
      + (d.pages ? ` · ${d.pages} pages` : "")
      + (d.favorites != null ? ` · ♥ ${fmtNum(d.favorites)}` : "")
      + (d.upload_date ? ` · ${fmtDate(d.upload_date)}` : "")),
  );

  // Grouped tag rows, nhentai-style.
  const groups = d.groups || {};
  for (const key of GROUP_ORDER) {
    const arr = groups[key];
    if (!arr || !arr.length) continue;
    root.appendChild(
      h("div", { class: "d-meta-row" },
        h("span", { class: "d-meta-label" }, (GROUP_LABELS[key] || key) + ":"),
        h("span", { class: "d-meta-tags" },
          ...arr.slice(0, 12).map(t =>
            h("span", { class: "d-tag" },
              t.name,
              t.count ? h("span", { class: "cnt" }, " " + fmtNum(t.count)) : null))),
      )
    );
  }
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
