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
import { renderTrendingTags, renderRecommendations } from "plugins/home-rows.js";  // v11.7
// v12.3: prefetchGallery import REMOVED — no more background detail warming.
// Card taps open a minimal sheet whose actions (Download/Save/Share) need no
// extra upstream fetches; detail data only loads when the sheet is open.

const PAGE_SIZE = 25;

export async function render(root, { me }) {
  // v12.1 (C): paginated mode uses a "Load next page" button on narrow
  // viewports (mobile — where the 429 storm was worst). Infinite scroll
  // stays on wide viewports. The user's manual chip-tap acknowledges each
  // new page, which naturally caps upstream load. Env-neutral: no build step.
  const PAGINATED = (typeof window !== "undefined")
    && !!(window.matchMedia && window.matchMedia("(max-width: 768px)").matches);

  const state = {
    query: "",
    parsed: parseSearch(""),
    // v12.11 (#2): default landing tab is "Popular Now" (popular-today),
    // not the older "Popular" firehose. buildChipRow already lists
    // Popular Now first (v12.10 #3), so activating this sort here makes
    // the very first grid paint match the very first visible chip.
    sort: "popular-today",
    page: 1,
    loading: false,
    done: false,
    results: [],
    token: 0,     // v12 (#1): bumped on every refetch — stale responses drop
    rerun: false, // v12 (#1): queued re-entry when load() is busy
    hasMore: false, // v12.1 (C): server-reported has_more; drives the button
    rateLimited: false, // v12.1 (C): last page hit upstream 429 — offer retry
    paginated: PAGINATED,
  };

  // v12 (#1): buildSearchBar needs toggleHomeRows — it was called from
  // commit() in v11.9 without being in scope, so EVERY typing/Enter event
  // threw ReferenceError before refetch() could fire. Pass it in.
  const $bar = buildSearchBar(state, refetch, toggleHomeRows);
  const $chips = buildChipRow(state, refetch);
  const $hint = h("div", { class: "search-hint" },
    "Try: ", h("code", {}, "tag:vanilla"), " ",
    h("code", {}, "-tag:yaoi"), " ",
    h("code", {}, "pages:>30"), " ",
    h("code", {}, "sort:popular-today"),
  );
  // v11.7: home widgets — visible only when there's no active query.
  const $trending = renderTrendingTags((tagExpr) => {
    if (!$bar) return;
    const input = $bar.querySelector("input");
    if (input) {
      input.value = tagExpr;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    haptic("light");
  });
  const $recs = renderRecommendations(me);
  const $home = h("div", { class: "home-rows" }, $trending, $recs);
  const $grid = h("div", { class: "card-grid" });
  const $footer = h("div", { class: "u-center", style: { padding: "24px 0" } });

  root.append($bar, $hint, $chips, $home, $grid, $footer);

  function toggleHomeRows() {
    const active = !!(state.query && state.query.trim());
    $home.style.display = active ? "none" : "block";
  }
  toggleHomeRows();

  // Initial load with a skeleton grid.
  showSkeleton();
  await load();

  // v12.1 (C): infinite scroll ONLY on wide viewports. On mobile we use a
  // "Load next page" button (rendered by renderFooter()) so each page is a
  // deliberate user action — that naturally caps prefetch storms.
  let io = null;
  if (!state.paginated) {
    io = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (e.isIntersecting && !state.loading && !state.done && state.hasMore) load();
      }
    }, { rootMargin: "600px 0px" });
    io.observe($footer);
  }

  function renderFooter() {
    $footer.innerHTML = "";
    if (state.loading) { $footer.textContent = "Loading…"; return; }

    // v12.11 (#3): the Next-Page button MUST render on every sort tab
    // regardless of what has_more said, as long as the current page
    // returned any rows and we're on a paginated viewport. In v12.10
    // (mobile), a False has_more from the backend used to collapse the
    // footer to '— end —' silently — which is exactly what the user
    // reported: "no next page option on any pages". Trust the results:
    // if we got a full-size page, we KNOW upstream has more.
    const showNextBtn = state.paginated
      && state.results.length > 0
      && (state.hasMore || state.results.length >= PAGE_SIZE * state.page - PAGE_SIZE || state.rateLimited);

    if (!state.hasMore && !showNextBtn && state.results.length > 0) {
      $footer.textContent = "— end —"; return;
    }
    if (!state.hasMore && state.results.length === 0) { $footer.textContent = ""; return; }

    if (state.paginated) {
      // Always render the button when we have results on a paginated
      // viewport. Label + haptic distinguish the two flavors.
      const btn = h("button", { class: "btn btn-primary next-page-btn",
        style: { padding: "12px 24px", fontWeight: "600", fontSize: "var(--du-fs-md)" },
      }, state.rateLimited ? "⚠ Rate-limited — tap to retry"
                            : `Load next page (→ page ${state.page})`);
      btn.addEventListener("click", () => { haptic("medium"); load(); });
      $footer.appendChild(btn);
    } else {
      $footer.textContent = state.rateLimited
        ? "Upstream rate-limited — scroll to retry"
        : "Scroll for more";
    }
  }

  function showSkeleton() {
    $grid.innerHTML = "";
    $grid.appendChild(make("skeleton", { variant: "card-grid", count: 6 }));
  }

  async function refetch() {
    state.token = (state.token || 0) + 1;  // v12: invalidate in-flight loads
    state.page = 1;
    state.done = false;
    state.results = [];
    showSkeleton();
    await load();
  }

  // v12 (#1): load() used to early-return when state.loading was true,
  // silently DROPPING the refetch that Enter/chips had just asked for
  // (grid stuck on skeletons forever). Now a busy load() queues one
  // re-entry instead, and every awaited response is dropped if its token
  // no longer matches — so the latest request always owns the grid.
  async function load() {
    // v12.11 (#3): honor state.done ONLY when we're sure it's end-of-results
    // (state.hasMore was false AND we already have rows). A user tapping the
    // Next-Page button must never no-op silently.
    if (state.done && !state.paginated) return;
    if (state.loading) { state.rerun = true; return; }
    state.loading = true;
    try {
      do {
        state.rerun = false;
        const token = state.token;
        renderFooter();  // v12.1 (C): unified "Loading…" via renderFooter
        let rows = null;
        try {
          const p = state.parsed;
          rows = await api.get("/api/search", {
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
        } catch (e) {
          if (token === state.token) {
            console.error("search load:", e);
            $footer.innerHTML = "";
            $footer.textContent = "Error: " + (e.message || "unknown");
          }
          continue;  // stale error → ignore; queued rerun re-loops fresh
        }
        if (token !== state.token) continue;  // stale response → never paint
        const items = rows.items || [];
        // v12.1 (B/C): use server-reported has_more instead of guessing from
        // items.length < PAGE_SIZE (which lied when the English filter dropped
        // most of an upstream page).
        state.hasMore = !!rows.has_more;
        state.rateLimited = !!rows.upstream_rate_limited;
        if (state.page === 1) $grid.innerHTML = "";
        for (const g of items) {
          $grid.appendChild(renderCard(g));
          // v12.3: prefetchGallery storm REMOVED entirely — the single
          // biggest contributor to the 429 flood in the Render log.
        }
        state.page += 1;
        state.done = !state.hasMore;
        renderFooter();
        if (state.results.length === 0 && items.length === 0) {
          $grid.appendChild(emptyState());
        }
        state.results.push(...items);
      } while (state.rerun && !state.done);
    } finally {
      state.loading = false;
      // v12.1 (C): the in-loop renderFooter() calls run while loading=true
      // and early-return with "Loading…". Re-render NOW so the paginated
      // "Load next page" button (or "— end —") actually replaces it.
      try { renderFooter(); } catch (_) { /* footer cosmetics only */ }
    }
  }

  function renderCard(g) {
    return make("card", {
      id: g.id, title: g.title, cover: g.cover, pages: g.pages,
      badge: null,  // v11.9 (#5): removed "Np" badge — covers stay clean
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

function buildSearchBar(state, refetch, toggleHomeRows) {
  const input = h("input", {
    type: "search", enterkeyhint: "search", inputmode: "search",
    placeholder: "Search galleries, tags, artists…",
    autocomplete: "off", autocapitalize: "off", spellcheck: "false",
    name: "q",
  });
  const clear = h("button", { type: "button", class: "search-clear u-hide", "aria-label": "Clear" }, "✕");
  // v12 (#1): the <form> IS the search bar now — a real block-level flex
  // container (same .search-bar CSS) instead of a `display: contents`
  // wrapper around an inner div. Telegram's Android WebView + several
  // IMEs never deliver a submit event to a display:contents form, which
  // is one reason the on-screen Search/Go key appeared dead.
  const form = h("form", {
    class: "search-bar",
    novalidate: "novalidate",
  },
    h("span", { class: "search-icon" }, "🔎"),
    input, clear,
  );

  let timer = null;
  let composing = false;
  let lastCommitted = null;  // v12: dedupe keydown+keyup+submit+change bursts

  function commit(immediate = false) {
    // v12: read input.value VERBATIM at commit time — never a cached
    // state.query (Android fires compositionend AFTER the Enter keydown).
    state.query = input.value;
    state.parsed = parseSearch(state.query);
    if (typeof toggleHomeRows === "function") toggleHomeRows();
    clear.classList.toggle("u-hide", !state.query);
    clearTimeout(timer);
    if (immediate) {
      // Identical Enter already committed and painted → no-op (this is
      // what makes the keydown/keyup/submit/change quadruple-fire safe).
      if (state.query === lastCommitted && (state.results.length || state.loading)) return;
      lastCommitted = state.query;
      haptic("select"); refetch();
    } else {
      timer = setTimeout(() => {
        lastCommitted = state.query;
        haptic("select"); refetch();
      }, 350);
    }
  }

  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend",   () => { composing = false; commit(false); });
  input.addEventListener("input", () => { if (!composing) commit(false); });
  const enterHit = (e) => {
    e.preventDefault();
    commit(true);            // fire NOW — no 350ms debounce on Enter
    input.blur();            // dismiss the keyboard
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === "Go" || e.keyCode === 13) enterHit(e);
  });
  // v12: Gboard/SwiftKey often swallow the action-key keydown entirely but
  // still deliver keyup — catch it there (deduped via lastCommitted).
  input.addEventListener("keyup", (e) => {
    if (e.key === "Enter" || e.key === "Go" || e.keyCode === 13) enterHit(e);
  });
  // v12: last-ditch fallback — some IMEs only fire `change` when the
  // search key commits/blurs the field.
  input.addEventListener("change", () => { if (!composing) commit(true); });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    commit(true);
    input.blur();
  });
  // iOS fires 'search' on the native clear affordance / IME search key.
  input.addEventListener("search", () => commit(true));

  clear.addEventListener("click", () => {
    input.value = ""; state.query = ""; state.parsed = parseSearch("");
    lastCommitted = "";
    clear.classList.add("u-hide"); refetch();
  });
  return form;
}

function buildChipRow(state, refetch) {
  const row = h("div", { class: "chip-row" });
  // v12.10 (#3+#5): order — Popular Now → New Uploads → Popular Week → Popular
  // Renamed: "Recent" → "New Uploads", "Popular Today" → "Popular Now".
  const opts = [
    { label: "⭐ Popular Now",   sort: "popular-today" },
    { label: "🆕 New Uploads",   sort: "date" },
    { label: "📅 Popular Week",  sort: "popular-week" },
    { label: "🔥 Popular",       sort: "popular" },
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

  // v11.7: Random button. Tag-aware when the user has bookmarks (uses their
  // top saved tags); falls back to popular when they don't.
  const randomChip = h("button", {
    class: "chip",
    style: {
      background: "linear-gradient(135deg, var(--du-accent), var(--du-accent-hover))",
      color: "var(--du-ink-inv)",
      border: "0", fontWeight: "600",
    },
    title: "Open a random gallery (uses your top tags when available)",
  }, "🎲 Random");
  randomChip.addEventListener("click", async () => {
    try { haptic("medium"); } catch (_) {}
    randomChip.disabled = true;
    const originalLabel = randomChip.textContent;
    randomChip.textContent = "🎲 Picking…";
    try {
      const g = await api.get("/api/random?respect_tags=1");
      if (g && g.id) {
        const m = await import("plugins/detail-sheet.js");
        m.openGalleryDetail(g, undefined);
        if (g._reason) {
          try { make("toast", { text: `🎲 Picked because ${g._reason}`, kind: "success" }); } catch (_) {}
        }
      } else {
        try { make("toast", { text: "No gallery available", kind: "error" }); } catch (_) {}
      }
    } catch (e) {
      try { make("toast", { text: "Random failed: " + (e.message || e), kind: "error" }); } catch (_) {}
    } finally {
      randomChip.disabled = false;
      randomChip.textContent = originalLabel;
    }
  });
  row.appendChild(randomChip);

  return row;
}
