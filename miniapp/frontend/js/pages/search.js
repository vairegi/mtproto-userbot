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
import { renderTrendingTags, renderRecommendations } from "plugins/home-rows.js?v=1.22.5";  // v11.7
// v12.3: prefetchGallery import REMOVED — no more background detail warming.
// Card taps open a minimal sheet whose actions (Download/Save/Share) need no
// extra upstream fetches; detail data only loads when the sheet is open.

const PAGE_SIZE = 25;
// v12.16: max numbered buttons in the pagination window (nhentai uses 7).
const PAGE_WINDOW = 7;

export async function render(root, { me }) {
  // v12.17: Unicode enclosed-circle glyphs for the ACTIVE page 1..9.
  // Pages 10+ keep a plain numeric label (there is no ordered set beyond
  // ❾ in these dingbats). Declared BEFORE state/renderFooter so it is
  // reachable from buildPaginationBar (which is a hoisted function).
  const ACTIVE_GLYPH = ["", "❶", "❷", "❸", "❹", "❺", "❻", "❼", "❽", "❾"];

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
  // v1.22.5: hint row shows BOT 1's scraped trending tags (tappable) under
  // the label "Try Searching These", falling back to the static operator
  // hints if the scraper list is unavailable. Tapping a tag fills the
  // search box with tag:<slug> and searches immediately.
  const $hint = h("div", { class: "search-hint" },
    "Try Searching These: ", h("code", {}, "tag:vanilla"), " ",
    h("code", {}, "-tag:yaoi"), " ",
    h("code", {}, "pages:>30"), " ",
    h("code", {}, "sort:popular-today"),
  );
  const _fillSearch = (expr) => {
    if (!$bar) return;
    const input = $bar.querySelector("input");
    if (input) {
      input.value = expr;
      input.dispatchEvent(new Event("input", { bubbles: true }));
    }
    haptic("light");
  };
  (async () => {
    let tags = [];
    try {
      const r = await api.get("/api/trending/scraper-tags?limit=10");
      tags = r.items || [];
    } catch (_) { tags = []; }
    if (!tags.length) return;  // keep the static fallback hints
    $hint.textContent = "";
    $hint.append("Try Searching These: ");
    tags.forEach((slug, i) => {
      const c = h("code", {
        style: { cursor: "pointer" },
        title: `Search tag:${slug}`,
      }, `tag:${slug}`);
      c.addEventListener("click", () => _fillSearch(`tag:${slug}`));
      $hint.append(c);
      if (i < tags.length - 1) $hint.append(" ");
    });
  })();
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
    // v12.17: › was unreachable when the backend returned has_more=false
    // after the English filter culled a page below per_page. Trust the
    // server's has_more when present, otherwise treat a FULL page
    // (>= PAGE_SIZE items) as a signal that a next page probably exists.
    // If the user taps and page N+1 turns out empty, emptyState() renders.
    const probableHasMore = state.hasMore
      || (Array.isArray(state.results) && state.results.length >= PAGE_SIZE);

    let lastKnown = probableHasMore ? cur + 1 : cur;
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
      const isActive = (p === cur);
      // v12.17: the ACTIVE page uses the ❶..❾ glyph so the user's
      // current position is immediately readable at a glance.
      const label = isActive && p >= 1 && p <= 9
        ? ACTIVE_GLYPH[p]
        : String(p);
      const b = h("button", {
        class: "du-page-btn" + (isActive ? " active" : ""),
        type: "button",
        "aria-label": "Page " + p,
      }, label);
      if (isActive) b.setAttribute("aria-current", "page");
      else b.addEventListener("click", () => { haptic("light"); goToPage(p); });
      bar.appendChild(b);
    }

    // v12.17: › follows probableHasMore — unreachable ONLY when we have
    // positive evidence there is no next page (returned < PAGE_SIZE items
    // AND the server said has_more=false).
    bar.appendChild(navBtn("›", cur + 1, !probableHasMore, "Next page"));
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
    // v1.22.7: reset the learned page bounds too. They leaked across sorts
    // (browsing "popular" to page 15 made the numbered bar show pages 3-15
    // on popular-today/week/date as well). Each sort/query must learn its
    // own bounds honestly.
    state.highestKnownPage = 0;
    state.knownLastPage = 0;
    await goToPage(1);
  }

  // v12.16: THE single navigation entry point. Fetches ONLY page n,
  // clears the grid, REPLACES state.results (never pushes), updates the
  // hash, re-renders the pagination bar.
  // v12.18: also resets the scroll position so the user lands on ITEM 1
  // of the new page instead of on whatever offset the OLD page left them
  // (bug repro: tap › while at the bottom of page 2 — page 3 paints,
  // but the viewport stays near the pagination bar and you have to
  // scroll up to see item 1).
  async function goToPage(n) {
    n = Math.max(1, n | 0);
    state.token = (state.token || 0) + 1;   // invalidate in-flight loads
    state.page = n;
    showSkeleton();
    _writeHashState(state);
    _scrollToTop();
    await load();
    // Re-assert scroll after paint — some WebViews restore the previous
    // offset once new content settles. Cheap and idempotent.
    _scrollToTop();
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
      is_cached: typeof g.is_cached === "boolean" ? g.is_cached : undefined,
      onOpen: () => openDetail(g),
    });
  }

  // v12.55: the inline detail sheet is GONE. Every page (all sorts, saved,
  // random, deep-links) now opens the SAME rich sheet from
  // plugins/detail-sheet.js — so any future sheet update applies everywhere.
  async function openDetail(g) {
    // Prefetch V2 dedup status so the Download button label is right on first paint.
    if (!g.v2_status) {
      api.get(`/api/gallery/${g.id}/status`)
        .then(st => { g.v2_status = st || { known: false }; })
        .catch(() => { g.v2_status = { known: false }; });
    }
    const m = await import("plugins/detail-sheet.js?v=12.57");
    m.openGalleryDetail(g, me);
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

// v12.18: reset the scroll position on page change so the user lands
// on item 1 of the new page. Telegram WebView wraps the app inside
// #app-main, so the true scroll container is that element (not window)
// on Android. Best-effort — every branch is wrapped in try/catch so
// the browserless test harness (no scrollTo) never throws.
export function _scrollToTop() {
  try { window.scrollTo?.({ top: 0, left: 0, behavior: "auto" }); } catch (_) {}
  try {
    const main = (typeof document !== "undefined")
      ? document.getElementById("app-main")
      : null;
    if (main && main.scrollTo) main.scrollTo({ top: 0, left: 0, behavior: "auto" });
    else if (main) main.scrollTop = 0;
  } catch (_) {}
  try {
    if (typeof document !== "undefined") {
      if (document.documentElement) document.documentElement.scrollTop = 0;
      if (document.body) document.body.scrollTop = 0;
    }
  } catch (_) {}
  // v12.18: record the call so tests can assert it fired without
  // needing a real DOM/viewport.
  try {
    if (typeof globalThis !== "undefined") {
      globalThis.__du_scrollToTop_calls = (globalThis.__du_scrollToTop_calls || 0) + 1;
    }
  } catch (_) {}
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

  // v12.20: SEARCH-ON-ENTER ONLY. Previously the "input" listener fired
  // commit(false) on every keystroke with a 350 ms debounce, so typing
  // "in" for "incest" would auto-search after a short pause and burn a
  // full upstream call for a 2-letter query. Removing that listener means
  // typed characters only update the visible clear-button state — no
  // network call, no cache write — until the user actually presses Enter,
  // submits the form, or the input fires `change` on blur.
  //
  // compositionstart/end are still tracked so composing IMEs don't get
  // half-committed if the user hits Enter mid-composition, but the
  // auto-commit on compositionend has been removed for the same reason
  // (a Chinese/Japanese user finishing a composition should not trigger
  // a search either — only Enter should).
  input.addEventListener("compositionstart", () => { composing = true; });
  input.addEventListener("compositionend",   () => { composing = false; });
  // Keep the clear-button visibility in sync with the input WITHOUT
  // committing / firing a search.
  input.addEventListener("input", () => {
    state.query = input.value;
    clear.classList.toggle("u-hide", !state.query);
  });
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
  // search key commits/blurs the field. Still gated on `composing` so a
  // stray blur during IME composition does not fire a partial search.
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
        const m = await import("plugins/detail-sheet.js?v=12.57");
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
