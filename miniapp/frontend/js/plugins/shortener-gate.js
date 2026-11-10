/* v13.15: action-only verification. No import-time request or overlay. */
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

async function _openVerify(btn, errorEl) {
  btn.disabled = true;
  btn.textContent = "⏳ Generating link…";
  errorEl.textContent = "";
  try {
    const r = await api.get("/api/shortener/link");
    if (r?.verified === true) { _removeOverlay(); return; }
    if (!r?.url) throw new Error("Verification link unavailable. Please retry later.");
    const tg = window.Telegram && window.Telegram.WebApp;
    if (tg?.openLink) tg.openLink(r.url);
    else window.open(r.url, "_blank");
  } catch (e) {
    errorEl.textContent = e?.message || "Link unavailable. Access remains locked; retry later.";
  } finally {
    btn.disabled = false;
    btn.textContent = "🔓 Verify & Unlock";
  }
}

function _buildOverlay(cfg) {
  const overlay = h("div", {
    class: "sg-overlay", role: "dialog", "aria-modal": "true",
    "aria-label": "Verification required",
  });
  const card = h("div", { class: "sg-card" });

  const cancel = h("button", {
    class: "sg-btn sg-btn-secondary", type: "button",
    "aria-label": "Close verification and continue browsing",
  }, "← Continue browsing");
  cancel.addEventListener("click", _removeOverlay);
  card.appendChild(cancel);
  card.appendChild(h("div", { class: "sg-icon" }, "🔐"));
  card.appendChild(h("div", { class: "sg-message" },
    (cfg.message && cfg.message.trim()) || "Verification required."));

  // Hardcoded primary unlock button — always present for locked users.
  const unlockBtn = h("button", {
    class: "sg-btn sg-btn-primary", type: "button",
  }, cfg.unlock_button || "🔓 Verify & Unlock");
  const errorEl = h("div", { class: "sg-hint", role: "alert" });
  unlockBtn.addEventListener("click", () => { _openVerify(unlockBtn, errorEl); });
  card.appendChild(unlockBtn);
  card.appendChild(errorEl);

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
    "Complete the link in your Telegram DM — this window closes automatically. Then tap Read or Download again."));

  overlay.appendChild(card);
  return overlay;
}

let _checking = null;

async function _checkAccess() {
  if (_overlay) return false;
  let cfg;
  try {
    cfg = await api.get("/api/shortener/status?prompt=true");
  } catch (_) {
    // Never interpret a failed check as permission to Read / Download.
    cfg = { locked: true, message: "Verification status unavailable. Please retry later or continue browsing." };
  }
  if (!cfg || cfg.locked !== true) return true;
  _overlay = _buildOverlay(cfg);
  document.body.classList.add("sg-locked");
  document.body.appendChild(_overlay);
  const current = _overlay;
  _pollTimer = setInterval(async () => {
    try {
      const c = await api.get("/api/shortener/status");
      if (_overlay === current && c && c.locked === false) _removeOverlay();
    } catch (_) { /* retry while dialog is open */ }
  }, POLL_MS);
  return false; // after verification tap the desired action again
}

export async function ensureShortenerAccess() {
  if (!_checking) {
    _checking = _checkAccess().finally(() => { _checking = null; });
  }
  return _checking;
}
