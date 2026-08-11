/*
  pages/search.js — Discover / Search page

  Renders:
    - smart search bar (uses plugins/search-operators.js)
    - filter chip row (English is baked in; chips just toggle sort presets)
    - grid of cards (uses components/card.js via components registry)
    - strict nhentai-style page picker (« ‹ 1..7 › ») — v12.16
    - tap a card → detail sheet whose buttons come from plugins/card-actions.js

  v12.16: accumulation (infinite scroll / "Load next page" append) REMOVED.
  Every navigation goes through goToPage(n), which fetches ONLY page n,
  clears the grid, and REPLACES state.results. This keeps DOM + memory flat
  no matter how deep the user pages — critical on 512 MB Render + WebViews.
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
// v12.16: max numbered buttons in the pagination window (nhentai uses 7).
const PAGE_WINDOW = 7;

export async function render(root, { me }) {
  const state = {
    query: "",
    parsed: parseSearch(""),
    // v12.11 (#2): default landing tab is "Popular Now" (popular-today).
    sort: "popular-today",
    page: 1,
    loading: false,
    results: [],
    token: 0,            // v12 (#1): bumped on every refetch — stale responses drop
    rerun: false,        // v12 (#1): queued re-entry when load() is busy
    hasMore: false,      // server-reported has_more for the CURRENT page
    rateLimited: false,  // last page hit upstream 429 — offer retry
    // v12.16: two DISTINCT learned bounds (do NOT conflate — v12.16 bug):
    //   highestKnownPage — highest page number we have POSITIVE evidence
    //                      exists (grows whenever has_more=true or a page
    //                      loads). Used to size the numbered window.
    //   knownLastPage    — the actual LAST page, learned ONLY when a fetch
    //                      returns has_more === false. » jumps here; it is
    //                      disabled until this is honestly known.
    highestKnownPage: 0,
    knownLastPage: 0,
  };

  // v12.16: hash restore — parse page/sort/q out of location.hash so a
  // back-button or a pasted link lands on the exact page the user left.
  // Format: #search?page=4&sort=popular-week&q=tag:vanilla
  _applyHashState(state);

  // v12 (#1): buildSearchBar needs toggleHomeRows — it was called from
  // commit() in v11.9 without being in scope, so EVERY typing/Enter event
  // threw ReferenceError before refetch() could fire. Pass it in.
  const $bar = buildSearchBar(state, refetch, toggleHomeRows);
  // v12.16: if the hash carried a query, prefill the input so the visible
  // search box matches the restored results.
  if (state.query) {
    const inp = $bar.querySelector("input");
    if (inp) { inp.value = state.query; }
  }
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
  const $footer = h("div", { class: "u-center", style: { padding: "16px 0 24px" } });

  root.append($bar, $hint, $chips, $home, $grid, $footer);

  function toggleHomeRows() {
    const active = !!(state.query && state.query.trim());
    $home.style.display = active ? "none" : "block";
  }
  toggleHomeRows();

  // v12.16: react to back/forward hash changes that target the search page.
  // The router re-renders the whole page on hashchange (routeTo), which
  // already restores state via _applyHashState — so this listener only
  // matters when the hash changes WITHOUT a routeTo re-render (defensive).
  // The returned teardown unregisters it.
  const onHashChange = () => {
    const h0 = (location.hash || "");
    if (!/^#?\/?search/i.test(h0)) return;  // not our page
    const before = state.page;
    _applyHashState(state);
    if (state.page !== before) goToPage(state.page);
  };
  window.addEventListener("hashchange", onHashChange);

  // Initial load with a skeleton grid.
  showSkeleton();
  await load();

  // v12.16: IntersectionObserver infinite scroll REMOVED entirely.
  // v12.16: mobile "Load next page" button REMOVED — pagination bar below
  // replaces both accumulation modes on every viewport.

  /* ------------------------------------------------------------------ */
  /* Pagination bar (v12.16)                                             */
  /* ------------------------------------------------------------------ */

  function renderFooter() {
    $footer.innerHTML = "";
    if (state.loading) { $footer.textContent = "Loading…"; return; }

    // No results at all → no bar, just the empty state (grid shows it).
    if (state.results.length === 0 && !state.hasMore && state.page === 1) {
      $footer.textContent = "";
      return;
    }

    // Rate-limit note rides above the bar when present.
    if (state.rateLimited) {
      $footer.appendChild(h("div", {
        class: "du-rate-note",
        style: { marginBottom: "8px" },
      }, "⚠ Upstream rate-limited — some results may be missing. Retry the page."));
    }

    $footer.appendChild(buildPaginationBar());
  }

  function buildPaginationBar() {
    const cur = state.page;
    // Highest page we can offer a numbered button for: when has_more is
    // true at least cur+1 exists; otherwise cur is the end. Fold in the
    // learned high-water mark and (if known) the real last page.
    let lastKnown = state.hasMore ? cur + 1 : cur;
    if (state.highestKnownPage > lastKnown) lastKnown = state.highestKnownPage;
    if (state.knownLastPage > lastKnown) lastKnown = state.knownLastPage;

    // Numbered window: ≤ PAGE_WINDOW buttons, centered on current page,
    // clamped to [1, lastKnown].
    let start = Math.max(1, cur - Math.floor(PAGE_WINDOW / 2));
    let end = Math.min(lastKnown, start + PAGE_WINDOW - 1);
    start = Math.max(1, end - PAGE_WINDOW + 1);

    const bar = h("nav", {
      class: "du-pagination",
      role: "navigation",
      "aria-label": "Pages",
    });

    const navBtn = (label, target, disabled, ariaLabel) => {
      const b = h("button", {
        class: "du-page-btn du-page-nav",
        type: "button",
        "aria-label": ariaLabel,
      }, label);
      if (disabled) b.disabled = true;
      else b.addEventListener("click", () => { haptic("light"); goToPage(target); });
      return b;
    };

    bar.appendChild(navBtn("«", 1, cur === 1, "First page"));
    bar.appendChild(navBtn("‹", cur - 1, cur === 1, "Previous page"));

    for (let p = start; p <= end; p++) {
      const b = h("button", {
        class: "du-page-btn" + (p === cur ? " active" : ""),
        type: "button",
        "aria-label": "Page " + p,
      }, String(p));
      if (p === cur) b.setAttribute("aria-current", "page");
      else b.addEventListener("click", () => { haptic("light"); goToPage(p); });
      bar.appendChild(b);
    }

    // › next: disabled only when we KNOW there is no next page
    // (has_more false on the current page).
    bar.appendChild(navBtn("›", cur + 1, !state.hasMore, "Next page"));
    // » last: enabled ONLY once a real end has been observed
    // (knownLastPage > 0) and the user is not already on it.
    const lastBtnDisabled = !state.knownLastPage || cur >= state.knownLastPage;
    bar.appendChild(navBtn("»", state.knownLastPage || cur, lastBtnDisabled, "Last page"));

    return bar;
  }

  /* ------------------------------------------------------------------ */
  /* Loading                                                             */
  /* ------------------------------------------------------------------ */

  function showSkeleton() {
    $grid.innerHTML = "";
    $grid.appendChild(make("skeleton", { variant: "card-grid", count: 6 }));
  }

  // v12.16: query/sort changes reset to page 1 — a strict page replace.
  async function refetch() {
    await goToPage(1);
  }

  // v12.16: THE single navigation entry point. Fetches ONLY page n,
  // clears the grid, REPLACES state.results (never pushes), updates the
  // hash, re-renders the pagination bar.
  async function goToPage(n) {
    n = Math.max(1, n | 0);
    state.token = (state.token || 0) + 1;   // invalidate in-flight loads
    state.page = n;
    showSkeleton();
    _writeHashState(state);
    await load();
  }

  // v12 (#1): load() used to early-return when state.loading was true,
  // silently DROPPING the refetch that Enter/chips had just asked for
  // (grid stuck on skeletons forever). Now a busy load() queues one
  // re-entry instead, and every awaited response is dropped if its token
  // no longer matches — so the latest request always owns the grid.
  async function load() {
    if (state.loading) { state.rerun = true; return; }
    state.loading = true;
    try {
      do {
        state.rerun = false;
        const token = state.token;
        renderFooter();  // "Loading…" while in flight
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
        state.hasMore = !!rows.has_more;
        state.rateLimited = !!rows.upstream_rate_limited;

        // v12.16 STRICT REPLACEMENT — the core of the whole task:
        //   1. ALWAYS clear the grid (not just on page 1).
        //   2. state.results is ASSIGNED, never pushed into.
        //   3. state.page is NEVER incremented here — goToPage owns it.
        $grid.innerHTML = "";
        for (const g of items) {
          $grid.appendChild(renderCard(g));
        }
        state.results = items;

        // v12.16: update the two learned bounds.
        //  - has_more=true  → page+1 provably exists → bump high-water mark.
        //  - has_more=false → this page IS the honest last page → record it.
        if (state.hasMore) {
          if (state.page + 1 > state.highestKnownPage) {
            state.highestKnownPage = state.page + 1;
          }
        } else if (state.page > 0) {
          state.knownLastPage = state.page;
          if (state.page > state.highestKnownPage) {
            state.highestKnownPage = state.page;
          }
        }

        if (items.length === 0) {
          $grid.appendChild(emptyState());
        }
        renderFooter();
      } while (state.rerun);
    } finally {
      state.loading = false;
      // Re-render so the real bar replaces "Loading…".
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

  return () => {
    // v12.16: no IntersectionObserver to disconnect anymore.
    window.removeEventListener("hashchange", onHashChange);
  };
}

/* ---------------------------------------------------------------------- */
/* Hash persistence (v12.16)                                               */
/* ---------------------------------------------------------------------- */

// Parse #search?page=N&sort=S&q=Q into state. Called on mount and on
// hashchange. Unknown / missing params fall back to current defaults.
function _applyHashState(state) {
  const raw = (typeof location !== "undefined" ? location.hash : "") || "";
  const qIdx = raw.indexOf("?");
  if (qIdx < 0) return;
  let params;
  try { params = new URLSearchParams(raw.slice(qIdx + 1)); }
  catch (_) { return; }

  const p = parseInt(params.get("page") || "", 10);
  if (Number.isFinite(p) && p >= 1) state.page = p;

  const s = params.get("sort");
  if (s && typeof s === "string") {
    state.sort = s;
    if (state.parsed) state.parsed.sort = s;
  }

  const q = params.get("q");
  if (q !== null && q !== undefined) {
    state.query = q;
    state.parsed = parseSearch(q);
    // An explicit q in the hash wins over a bare sort param.
    if (state.parsed && state.parsed.sort) state.sort = state.parsed.sort;
  }
}

// Write state back into the hash WITHOUT triggering a router re-render:
// use history.replaceState so the URL updates silently (no hashchange
// event, no routeTo, no page teardown).
function _writeHashState(state) {
  if (typeof history === "undefined" || !history.replaceState) return;
  const params = new URLSearchParams();
  params.set("page", String(state.page));
  params.set("sort", (state.parsed && state.parsed.sort) || state.sort || "popular-today");
  const q = (state.query || "").trim();
  if (q) params.set("q", q);
  const url = "#search?" + params.toString();
  try { history.replaceState(null, "", url); } catch (_) { /* best-effort */ }
}

/* ---------------------------------------------------------------------- */
/* Search bar (unchanged from v12.15 except refetch → page-1 reset)        */
/* ---------------------------------------------------------------------- */

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
        // v12.16: a sort change is a new result set → reset to page 1.
        // refetch() routes through goToPage(1).
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
