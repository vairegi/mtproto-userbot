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
    const actions = cardActions
      .filter(a => !a.when || a.when({ gallery: g, me }))
      .map(a => ({
        label: `${a.icon} ${a.label}`,
        kind: a.kind || "secondary",
        block: false,
        onClick: (sheetApi) => a.run({ gallery: g, me, close: sheetApi.close }),
      }));

    const body = h("div", {},
      g.cover ? h("img", {
        src: g.cover, alt: g.title,
        style: { width: "60%", maxWidth: "220px", margin: "0 auto",
                 display: "block", borderRadius: "12px",
                 border: "1px solid var(--du-border)" },
      }) : null,
      h("div", { style: { textAlign: "center", marginTop: "12px",
                          fontSize: "15px", fontWeight: "600" } }, g.title || `#${g.id}`),
      h("div", { style: { textAlign: "center", color: "var(--du-ink-lo)",
                          fontSize: "13px", marginTop: "4px" } },
        `#${g.id} · ${g.pages || "?"} pages`),
      g.tags && g.tags.length
        ? h("div", { class: "chip-row", style: { justifyContent: "center" } },
            ...g.tags.slice(0, 8).map(t =>
              h("span", { class: "chip", "aria-pressed": "false" }, t.name || t)))
        : null,
    );

    const sheet = make("sheet", { title: "Gallery", body, actions });
    sheet.open();
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
