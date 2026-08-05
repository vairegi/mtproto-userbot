/*
  pages/search.js — Discover / Search page

  Renders:
    - smart search bar (uses plugins/search-operators.js)
    - filter chip row (English is baked in; chips just toggle sort presets)
    - grid of cards (uses components/card.js via components registry)
    - infinite scroll on the grid
    - tap a card → detail sheet whose buttons come from plugins/card-actions.js
*/

import { api } from "core/api.js";
import { make, h } from "core/components.js";
import { haptic } from "core/telegram.js";
import { store } from "core/state.js";
import { parseSearch } from "plugins/search-operators.js";
import { cardActions } from "plugins/card-actions.js";

const PAGE_SIZE = 25;

export async function render(root, { me }) {
  const state = {
    query: "",
    parsed: parseSearch(""),
    sort: "popular",
    page: 1,
    loading: false,
    done: false,
    results: [],
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

  // Initial load with a skeleton grid.
  showSkeleton();
  await load();

  // Infinite scroll — grow when user hits the bottom.
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting && !state.loading && !state.done) load();
    }
  }, { rootMargin: "600px 0px" });
  io.observe($footer);

  function showSkeleton() {
    $grid.innerHTML = "";
    $grid.appendChild(make("skeleton", { variant: "card-grid", count: 6 }));
  }

  async function refetch() {
    state.page = 1;
    state.done = false;
    state.results = [];
    showSkeleton();
    await load();
  }

  async function load() {
    if (state.loading || state.done) return;
    state.loading = true;
    $footer.textContent = "Loading…";
    try {
      const p = state.parsed;
      const rows = await api.get("/api/search", {
        q: p.q,
        include_tags: p.include_tags.join(","),
        exclude_tags: p.exclude_tags.join(","),
        artist: p.artist || "",
        pages_min: p.pages_min || "",
        pages_max: p.pages_max || "",
        sort: p.sort || state.sort,
        lang: p.lang || "english",
        page: state.page,
        per_page: PAGE_SIZE,
      });
      const items = rows.items || [];
      if (state.page === 1) $grid.innerHTML = "";
      for (const g of items) $grid.appendChild(renderCard(g));
      state.page += 1;
      state.done = items.length < PAGE_SIZE;
      $footer.textContent = state.done ? "— end —" : "Scroll for more";
      if (state.results.length === 0 && items.length === 0) {
        $grid.appendChild(emptyState());
      }
      state.results.push(...items);
    } catch (e) {
      console.error("search load:", e);
      $footer.textContent = "Error: " + (e.message || "unknown");
    } finally {
      state.loading = false;
    }
  }

  function renderCard(g) {
    return make("card", {
      id: g.id, title: g.title, cover: g.cover, pages: g.pages,
      badge: g.pages ? `${g.pages}p` : null,
      onOpen: () => openDetail(g),
    });
  }

  function openDetail(g) {
    // Fetch V2 dedup status once, in the background, so the sheet re-renders
    // its primary button as "Open Post" / "Downloading…" / "Queue to Channel".
    // The sheet appears immediately; the status update follows within one RTT.
    if (!g.v2_status) {
      api.get(`/api/gallery/${g.id}/status`)
        .then(s => { g.v2_status = s || { known: false }; })
        .catch(() => { g.v2_status = { known: false }; });
    }

    // Turn cardActions entries into sheet-button descriptors, unwrapping
    // function-valued label / icon / disabled fields (V2 dynamic actions).
    const _val = (v, ctx) => (typeof v === "function" ? v(ctx) : v);
    const actions = cardActions
      .filter(a => !a.when || a.when({ gallery: g, me }))
      .map(a => {
        const ctx = { gallery: g, me };
        return {
          label:    `${_val(a.icon, ctx)} ${_val(a.label, ctx)}`,
          kind:     a.kind || "secondary",
          block:    false,
          disabled: _val(a.disabled, ctx) || false,
          onClick:  (sheetApi) => a.run({ gallery: g, me, close: sheetApi.close }),
        };
      });

    // ---- detail-sheet body --------------------------------------------------
    // Shows cover + clean title immediately, then swaps in the full caption
    // (titles, grouped tags, favorites, upload date) when /api/gallery/{id}
    // returns. The sheet content is rebuilt in place so there's no flash.
    const GROUP_ORDER = ["parody", "character", "artist", "group", "language", "category", "tag"];
    const GROUP_LABEL = {
      parody: "Parody", character: "Characters", artist: "Artist",
      group: "Circle", language: "Language", category: "Category", tag: "Tags",
    };
    const fmtNum = (n) => {
      n = parseInt(n || 0, 10);
      if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
      if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
      return String(n || 0);
    };

    const $body = h("div", { class: "d-root" });

    function renderBase() {
      $body.innerHTML = "";
      $body.append(
        g.cover ? h("img", {
          class: "d-cover", src: g.cover, alt: g.title || "",
          loading: "lazy",
        }) : null,
        h("div", { class: "d-title" }, g.title || `#${g.id}`),
        h("div", { class: "d-sub" }, `#${g.id} · ${g.pages || "?"} pages`),
        h("div", { class: "d-loading" }, "Loading details…"),
      );
    }

    function renderFull(d) {
      // d is the detail payload from GET /api/gallery/{id}
      const groups = d.tag_groups || {};
      $body.innerHTML = "";

      const coverSrc = d.cover || g.cover;
      if (coverSrc) {
        $body.append(h("img", { class: "d-cover", src: coverSrc, alt: d.title || "" }));
      }

      // Bold clean title, then the FULL original titles as subtitles.
      $body.append(h("div", { class: "d-title" }, d.title || g.title || `#${g.id}`));
      if (d.title_english && d.title_english !== d.title) {
        $body.append(h("div", { class: "d-full-title" }, d.title_english));
      }
      if (d.title_japanese) {
        $body.append(h("div", { class: "d-jpn-title" }, d.title_japanese));
      }

      // Meta line: id · pages · ♥ favorites · upload date
      const metaBits = [`#${d.id || g.id}`];
      if (d.pages || g.pages) metaBits.push(`${d.pages || g.pages} pages`);
      if (d.favorites) metaBits.push(`♥ ${fmtNum(d.favorites)}`);
      if (d.upload_date) metaBits.push(d.upload_date);
      if (d.scanlator) metaBits.push(`scans: ${d.scanlator}`);
      $body.append(h("div", { class: "d-sub" }, metaBits.join("  ·  ")));

      // Grouped tag rows with labels — this is the "caption" the user asked for.
      for (const typ of GROUP_ORDER) {
        const names = groups[typ];
        if (!names || !names.length) continue;
        $body.append(h("div", { class: "d-meta-row" },
          h("span", { class: "d-meta-label" }, GROUP_LABEL[typ] + ":"),
          h("div", { class: "d-meta-tags" },
            ...names.slice(0, 12).map(n => h("span", { class: "d-tag" }, n))),
        ));
      }

      // Fallback: if the API gave no groups but the card had tags, show them.
      if (!Object.keys(groups).length && g.tags && g.tags.length) {
        $body.append(h("div", { class: "d-meta-row" },
          h("span", { class: "d-meta-label" }, "Tags:"),
          h("div", { class: "d-meta-tags" },
            ...g.tags.slice(0, 12).map(t => h("span", { class: "d-tag" }, t.name || t))),
        ));
      }
    }

    renderBase();

    const sheet = make("sheet", { title: "Gallery", body: $body, actions });
    sheet.open();

    // Load the full caption in the background and rebuild the body in place.
    api.get(`/api/gallery/${g.id}`)
      .then(d => { if (d && d.id) renderFull(d); else $body.querySelector(".d-loading")?.remove(); })
      .catch(() => { $body.querySelector(".d-loading")?.remove(); });
  }

  function emptyState() {
    return h("div", { class: "empty", style: { gridColumn: "1 / -1" } },
      h("div", { class: "icon" }, "🔍"),
      h("div", { class: "title" }, "No results"),
      h("div", {}, "Try a different query or clear filters."),
    );
  }

  return () => io.disconnect();
}

function buildSearchBar(state, refetch) {
  const input = h("input", {
    type: "search", enterkeyhint: "search",
    placeholder: "Search galleries, tags, artists…",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false",
  });
  const clear = h("button", { class: "search-clear u-hide", "aria-label": "Clear" }, "✕");
  const bar = h("div", { class: "search-bar" },
    h("span", { class: "search-icon" }, "🔎"),
    input, clear,
  );
  let timer = null;
  input.addEventListener("input", () => {
    state.query = input.value;
    state.parsed = parseSearch(state.query);
    clear.classList.toggle("u-hide", !state.query);
    clearTimeout(timer);
    timer = setTimeout(() => { haptic("select"); refetch(); }, 350);
  });
  clear.addEventListener("click", () => {
    input.value = ""; state.query = ""; state.parsed = parseSearch("");
    clear.classList.add("u-hide"); refetch();
  });
  return bar;
}

function buildChipRow(state, refetch) {
  const row = h("div", { class: "chip-row" });
  const opts = [
    { label: "🔥 Popular",       sort: "popular" },
    { label: "📅 Popular Week",  sort: "popular-week" },
    { label: "⭐ Popular Today", sort: "popular-today" },
    { label: "🆕 Recent",        sort: "date" },
  ];
  for (const o of opts) {
    const chip = make("chip", {
      label: o.label,
      active: state.sort === o.sort,
      onChange: () => {
        for (const c of row.querySelectorAll(".chip")) c.setAttribute("aria-pressed", "false");
        chip.setAttribute("aria-pressed", "true");
        state.sort = o.sort;
        refetch();
      },
    });
    if (state.sort === o.sort) chip.setAttribute("aria-pressed", "true");
    row.appendChild(chip);
  }
  return row;
}
