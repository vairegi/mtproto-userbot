/*
  app.js — Boot sequence

  Runs once on page load. Responsibilities:
    1. Register built-in components (side-effect imports)
    2. Boot Telegram SDK (theme sync, expand)
    3. Fetch caller identity/permissions from backend
    4. Render header + tab bar based on page registry
    5. Load initial page (from URL hash, default "search")
    6. Wire hash-based routing so back/forward works

  Component side-effect imports live HERE (not in core/components.js)
  because putting them in components.js creates a circular import graph
  that hits a Temporal Dead Zone error on Android Telegram WebView.
*/

// ---- IMPORTANT: preload built-in components BEFORE anything else --------
// Each of these files calls register("name", factory) at top level, which
// mutates the registry in core/components.js. By importing them here,
// AFTER components.js is fully initialized, we avoid the TDZ crash.
import "components/card.js";
import "components/chip.js";
import "components/sheet.js";
import "components/toast.js";
import "components/skeleton.js";

import { bootTelegram, haptic, showBackButton } from "core/telegram.js";
import { api } from "core/api.js";
import { store } from "core/state.js";
import { prefs } from "core/prefs.js";
import { pages, findPage } from "core/registry.js";
import { h } from "core/components.js";

const $header  = document.getElementById("app-header");
const $main    = document.getElementById("app-main");
const $tabbar  = document.getElementById("app-tabbar");

let currentPage = null;
let currentTeardown = null;

async function boot() {
  const tg = bootTelegram();
  store.set("tg", tg);

  // Ask backend who we are + what features are enabled for us.
  // If backend is down we still render the shell in "offline" mode.
  let me = { user_id: null, is_admin: false, public_mode: true, rate_limit: null };
  try {
    me = await api.get("/api/profile/me");
  } catch (e) {
    console.warn("profile/me failed:", e);
    store.set("boot_error", e.message || String(e));
  }
  store.set("me", me);

  // v12.10 (#6+#7): fetch the admin-configured card-grid layout BEFORE the
  // first page renders so the grid paints with the right columns/gap from
  // frame one. Sets --du-cards-per-row / --du-card-gap on :root; the CSS
  // (components.css .card-grid) reads them with sane fallbacks (2 / 0).
  // Fire-and-forget-ish: a failed fetch leaves the theme.css defaults.
  try {
    const layout = await api.get("/api/layout");
    const root = document.documentElement;
    if (layout && Number.isFinite(+layout.cards_per_row)) {
      root.style.setProperty("--du-cards-per-row", String(+layout.cards_per_row));
    }
    if (layout && Number.isFinite(+layout.card_gap)) {
      root.style.setProperty("--du-card-gap", String(+layout.card_gap));
    }
    // v12.11 (#4): horizontal page padding (admin-controlled left/right).
    if (layout && Number.isFinite(+layout.app_pad_x)) {
      root.style.setProperty("--du-app-pad-x", String(+layout.app_pad_x) + "px");
    }
    store.set("layout", layout || null);
  } catch (e) {
    console.warn("layout fetch failed (defaults apply):", e);
  }

  // v12.3: admin-configurable popup. Runs ONCE per mini-app open,
  // throttled server-side per user by /popuptime (default 2 hours).
  // Fire-and-forget — never block boot on a popup fetch failure.
  try { _maybeShowPopup(); } catch (_) { /* popup is best-effort */ }

  // v11.6: apply the server-side default background theme, but ONLY when
  // the user has never touched the Theme selector. "Never touched" means
  // the local pref still equals a factory default: v11.6's "dark", or the
  // legacy "ember" alias (users who upgraded from v10/v11 will still have
  // "ember" persisted). The admin sectionBackground UI is gone in v11.6,
  // so this only matters for deployments where the setting was last
  // written by an older admin build.
  try {
    const p = await api.get("/api/profile/preferences");
    const serverBg = (p && p.default_background_theme) || "";
    const localBg = prefs.get("background_theme");
    const isFactoryDefault = (!localBg || localBg === "dark" || localBg === "ember");
    if (serverBg && isFactoryDefault && serverBg !== localBg) {
      document.documentElement.dataset.bgTheme = serverBg;
      // Note: we deliberately do NOT prefs.set() here — the server
      // default should not overwrite the user's stored preference, it
      // only styles this session until the user picks their own.
    }
  } catch (e) {
    // Preferences endpoint down — keep whatever the local pref applied.
  }

  renderHeader(me);
  renderTabbar(me);

  // v11.8 (#4): deep-link handling. Supports BOTH:
  //   * Telegram start_param: t.me/<bot>/app?startapp=g_<id>
  //   * Hash route:           #/gallery/<id>  (for external browsers)
  window.addEventListener("hashchange", () => {
    const gid = _galleryHashId();
    if (gid) _openGalleryFromDeepLink(gid, store.get("me"));
    else     routeTo(hashPageId());
  });
  routeTo(hashPageId());
  try {
    const startParam = window.Telegram?.WebApp?.initDataUnsafe?.start_param;
    if (startParam && /^g_\d+$/.test(startParam)) {
      _openGalleryFromDeepLink(startParam.slice(2), me);
    } else {
      const gid = _galleryHashId();
      if (gid) _openGalleryFromDeepLink(gid, me);
    }
  } catch (_) { /* deep-linking is best-effort */ }

  // Signal to index.html's error-surface script that boot succeeded so it
  // doesn't overwrite the app with a "still loading" fallback message.
  if ($main) $main.dataset.booted = "1";
}

/* v12.3 — admin popup modal ---------------------------------------------
   Fetches GET /api/popup on boot. When show=true, renders a modal with:
     - the admin's message (from /popupmsg)
     - the admin's image (from /popupmsg with photo attached)
     - a × close button top-right
   Records the view via POST /api/popup/ack so the per-user throttle
   (/popuptime) is honoured. No z-index games — the modal is fixed,
   full-viewport, and sits above everything else by construction.
*/
async function _maybeShowPopup() {
  let cfg;
  try {
    cfg = await api.get("/api/popup");
  } catch (_) {
    return;  // popup endpoint down → skip silently, boot unaffected
  }
  if (!cfg || cfg.show !== true) return;

  // Ack immediately so a crash mid-modal still counts as "shown" and the
  // user doesn't get re-prompted on their very next open.
  try { api.post("/api/popup/ack", {}); } catch (_) { /* ignore */ }

  const overlay = h("div", { class: "popup-overlay", role: "dialog",
                             "aria-modal": "true", "aria-label": "Announcement" });
  const card = h("div", { class: "popup-card" });
  const closeBtn = h("button", {
    class: "popup-close", "aria-label": "Close", type: "button",
  }, "×");

  const dismiss = () => {
    try { haptic("light"); } catch (_) {}
    overlay.remove();
  };
  closeBtn.addEventListener("click", dismiss);
  overlay.addEventListener("click", (e) => { if (e.target === overlay) dismiss(); });

  if (cfg.has_image) {
    const img = h("img", { class: "popup-image", src: "/api/popup/image",
                           alt: "", loading: "eager" });
    card.appendChild(img);
  }
  if (cfg.message && cfg.message.trim()) {
    card.appendChild(h("div", { class: "popup-message" }, cfg.message));
  }
  card.appendChild(closeBtn);
  overlay.appendChild(card);
  document.body.appendChild(overlay);
}

function hashPageId() {
  const h = (location.hash || "").replace(/^#/, "").split("/")[0];
  return h || pages[0].id;
}

/* v11.8 (#4): deep-link helpers ----------------------------------------- */
function _galleryHashId() {
  const m = (location.hash || "").match(/^#\/?gallery\/(\d+)/i);
  return m ? m[1] : "";
}

async function _openGalleryFromDeepLink(gid, me) {
  if (!gid) return;
  try {
    const m = await import("plugins/detail-sheet.js?v=12.62");
    let g = { id: gid, title: `#${gid}` };
    try {
      const d = await api.get(`/api/gallery/${encodeURIComponent(gid)}`);
      if (d && d.id) g = d;
    } catch (_) { /* sheet will load details on its own */ }
    m.openGalleryDetail(g, me);
  } catch (e) {
    console.warn("deep-link open failed:", e);
  }
}

function renderHeader(me) {
  $header.innerHTML = "";
  const title = h("div", { class: "hdr-title u-grow u-truncate", id: "hdr-title" }, "Doujinshi Universe");
  const badgeQueue = h("span", { class: "hdr-badge", id: "queue-badge", title: "Live queue" }, "📥 0");
  const badgeMode = h("span", {
    class: "hdr-badge " + (me.public_mode ? "" : "warn"),
    title: me.public_mode ? "Public mode" : "Private (admin only)",
  }, me.public_mode ? "PUBLIC" : "PRIVATE");
  $header.appendChild(title);
  $header.appendChild(badgeMode);
  $header.appendChild(badgeQueue);

  // Live queue polling — 5s cadence.  This is the "live badge" feature.
  startQueuePolling();
}

function renderTabbar(me) {
  $tabbar.innerHTML = "";
  const visible = pages.filter(p => !p.adminOnly || me.is_admin);
  for (const p of visible) {
    const btn = h("button", {
      class: "tab-btn",
      dataset: { page: p.id },
      onclick: () => { haptic("light"); location.hash = "#" + p.id; },
    },
      h("span", { class: "tab-icon" }, p.icon),
      h("span", { class: "tab-label" }, p.label),
    );
    $tabbar.appendChild(btn);
  }
}

function highlightTab(id) {
  for (const b of $tabbar.querySelectorAll(".tab-btn")) {
    if (b.dataset.page === id) b.setAttribute("aria-current", "page");
    else                       b.removeAttribute("aria-current");
  }
}

async function routeTo(id) {
  const page = findPage(id);
  // If admin-only page requested by non-admin, fallback to default.
  const me = store.get("me", { is_admin: false });
  if (page.adminOnly && !me.is_admin) return routeTo(pages[0].id);

  // Teardown previous page.
  if (currentTeardown) { try { currentTeardown(); } catch (_) {} currentTeardown = null; }
  $main.innerHTML = "";
  // Boot succeeded once we've rendered anything — tell index.html's
  // fallback script to stand down.
  $main.dataset.booted = "1";

  // Set title.
  const $title = document.getElementById("hdr-title");
  if ($title) $title.textContent = page.title;
  highlightTab(page.id);

  // Dynamic import → render.
  try {
    const mod = await page.module();
    const container = h("div", { class: "page page-" + page.id });
    $main.appendChild(container);
    currentPage = page;
    const teardown = await mod.render(container, { me });
    currentTeardown = typeof teardown === "function" ? teardown : null;
  } catch (e) {
    console.error("page render failed:", e);
    $main.appendChild(h("div", { class: "empty" },
      h("div", { class: "icon" }, "⚠️"),
      h("div", { class: "title" }, "Failed to load page"),
      h("div", {}, String(e.message || e)),
    ));
  }
}

let queuePollTimer = null;
async function startQueuePolling() {
  const badge = document.getElementById("queue-badge");
  const tick = async () => {
    try {
      const s = await api.get("/api/queue/status");
      const total = (s.pending || 0) + (s.processing || 0);
      badge.textContent = "📥 " + total;
      badge.className = "hdr-badge" + (total > 0 ? " warn" : "");
      store.set("queue_status", s);
    } catch (_) { /* ignore transient errors */ }
  };
  tick();
  clearInterval(queuePollTimer);
  queuePollTimer = setInterval(tick, 5000);
}

boot();
