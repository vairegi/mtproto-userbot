/*
  pages/search.js — Discover / Search page

  Grid cards show cover + NAME only (bold). Tap → detail-sheet plugin
  fetches /api/gallery/{id} and renders the full metadata view.
*/

import { api } from "core/api.js";
import { make, h } from "core/components.js";
import { haptic } from "core/telegram.js";
import { parseSearch } from "plugins/search-operators.js";
import { openGalleryDetail } from "plugins/detail-sheet.js";

const PAGE_SIZE = 25;

export async function render(root, { me }) {
  const state = {
    query: "",
    parsed: parseSearch(""),
    sort: "popular",
    page: 1,
    loading: false,
    done: false,
  };

  const $bar = buildSearchBar(state, refetch);
  const $chips = buildChipRow(state, refetch);
  const $hint = h("div", { class: "search-hint" },
    "Try: ", h("code", {}, "tag:vanilla"), " ",
    h("code", {}, "-tag:yaoi"), " ",
    h("code", {}, "pages:>30"), " ",
    h("code", {}, "sort:popular"),
  );
  const $grid = h("div", { class: "card-grid" });
  const $footer = h("div", { class: "u-center", style: { padding: "24px 0" } });

  root.append($bar, $hint, $chips, $grid, $footer);

  showSkeleton();
  await load();

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting && !state.loading && !state.done) load();
    }
  }, { rootMargin: "600px 0px" });
