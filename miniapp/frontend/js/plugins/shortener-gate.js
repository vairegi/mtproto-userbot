/*
  shortener-gate.js — v13.0 link-shortener verification overlay.

  Runs FIRST on boot (before the popup, before any page render). When the
  backend says the user is locked, it paints a non-bypassable full-screen
  overlay and hides the whole app behind it:

      [ message ]                         (/shortenermsg — mini-app text)
      [ 🔓 Verify & Unlock ]   ← hardcoded primary, opens the short link
      [ secondary buttons… ]   ← admin-added via /shortenerbtn

  The overlay POLLS /api/shortener/status every few seconds; the moment the
  user completes verification in Telegram the backend flips verified=true
  and the overlay removes itself automatically (auto-clear).

  Fail-open: if the status fetch fails, the overlay is never shown and the
  app boots normally (the shortener is an earning layer, never a lockout).
*/

import { api } from "core/api.js";
import { h } from "core/components.js";

const POLL_MS = 4000;
let _pollTimer = null;
let _overlay = null;

function _removeOverlay() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_overlay) { try { _overlay.remove(); } catch (_) {} _overlay = null; }
  document.body.classList.remove("sg-locked");
}

async function _fetchLink() {
  // Mint a fresh token + shortened URL each time the user taps, so every
  // visit is a new payable hop.
  try {
    const r = await api.get("/api/shortener/link");
    if (r && r.url) return r.url;
    if (r && r.deep_link) return r.deep_link;
  } catch (_) { /* fall through */ }
  return "";
}

async function _openVerify(btn) {
  btn.disabled = true;
  btn.textContent = "⏳ Generating link…";
  const url = await _fetchLink();
  btn.disabled = false;
  btn.textContent = "🔓 Verify & Unlock";
  if (!url) return; // fail-open: backend said skip; overlay poll will clear
  const tg = window.Telegram && window.Telegram.WebApp;
  try {
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
  } catch (_) {
    try { window.open(url, "_blank"); } catch (_) {}
  }
}

function _buildOverlay(cfg) {
  const overlay = h("div", {
    class: "sg-overlay", role: "dialog", "aria-modal": "true",
    "aria-label": "Verification required",
  });
  const card = h("div", { class: "sg-card" });

  card.appendChild(h("div", { class: "sg-icon" }, "🔐"));
  card.appendChild(h("div", { class: "sg-message" },
    (cfg.message && cfg.message.trim()) || "Verification required."));

  // Hardcoded primary unlock button — always present for locked users.
  const unlockBtn = h("button", {
    class: "sg-btn sg-btn-primary", type: "button",
  }, cfg.unlock_button || "🔓 Verify & Unlock");
  unlockBtn.addEventListener("click", () => { _openVerify(unlockBtn); });
  card.appendChild(unlockBtn);

  // Optional admin-configured secondary buttons (label | url).
  if (Array.isArray(cfg.buttons) && cfg.buttons.length) {
    const row = h("div", { class: "sg-btn-row" });
    for (const b of cfg.buttons) {
      if (!b || !b.label || !b.url) continue;
      const el = h("button", { class: "sg-btn sg-btn-secondary", type: "button" },
        String(b.label));
      el.addEventListener("click", () => {
        const tg = window.Telegram && window.Telegram.WebApp;
        try {
          if (tg && tg.openLink) tg.openLink(String(b.url));
          else window.open(String(b.url), "_blank");
        } catch (_) { try { window.open(String(b.url), "_blank"); } catch (_) {} }
      });
      row.appendChild(el);
    }
    if (row.childNodes.length) card.appendChild(row);
  }

  card.appendChild(h("div", { class: "sg-hint" },
    "Complete the link in your Telegram DM — this window unlocks automatically."));

  overlay.appendChild(card);
  return overlay;
}

async function _check() {
  let cfg;
  try {
    cfg = await api.get("/api/shortener/status");
  } catch (_) {
    return; // fail open — never block boot on a gate fetch failure
  }
  if (!cfg || cfg.locked !== true) { _removeOverlay(); return; }

  if (!_overlay) {
    _overlay = _buildOverlay(cfg);
    document.body.classList.add("sg-locked");
    document.body.appendChild(_overlay);
  }

  if (!_pollTimer) {
    _pollTimer = setInterval(async () => {
      try {
        const c = await api.get("/api/shortener/status");
        if (c && c.locked === false) _removeOverlay(); // auto-clear after verify
      } catch (_) { /* keep overlay; retry next tick */ }
    }, POLL_MS);
  }
}

/* Boot the gate immediately on module import (app.js imports this plugin
   before rendering any page). Fire-and-forget — never throws. */
export function initShortenerGate() {
  try { _check(); } catch (_) { /* ignore */ }
}

// Auto-start on import so it always runs first.
initShortenerGate();
