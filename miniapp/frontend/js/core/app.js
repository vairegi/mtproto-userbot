/*
  app.js — Boot sequence

  Runs once on page load. Responsibilities:
    1. Boot Telegram SDK (theme sync, expand)
    2. Fetch caller identity/permissions from backend
    3. Render header + tab bar based on page registry
    4. Load initial page (from URL hash, default "search")
    5. Wire hash-based routing so back/forward works

  If you need to change GLOBAL behavior, edit here.  If you need to add a
  page, edit registry.js.  If you need to change a component, edit
  components.js.  If you need to change styling, edit theme.css.
*/

import { bootTelegram, haptic, showBackButton } from "core/telegram.js";
import { api } from "core/api.js";
import { store } from "core/state.js";
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

  renderHeader(me);
  renderTabbar(me);

  window.addEventListener("hashchange", () => routeTo(hashPageId()));
  routeTo(hashPageId());
}

function hashPageId() {
  const h = (location.hash || "").replace(/^#/, "").split("/")[0];
  return h || pages[0].id;
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
