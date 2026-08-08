/*
  pages/settings.js — Client-side preferences  (v11.7)

  v11.7 changes:
    * Two-row appearance section split cleanly:
        "Interface mode"  → theme_override  (auto / dark / light)
        "Palette"         → background_theme (10 options incl. auto)
      No more duplicate "Theme" label collision.
    * NEW: theme preview swatches — 3-color chip next to each palette
      option, rendered as a custom dropdown so <option> can carry SVG.
    * NEW: "Auto (system)" palette that follows the OS / Telegram
      colorScheme in real-time (see core/prefs.js resolvePalette()).
    * Palette changes are wired via prefs.set() ONLY — the DOM update is
      done inside prefs.apply() so themes.css picks it up immediately.
*/

import { h, make } from "core/components.js";
import { prefs, resolvePalette } from "core/prefs.js";
import { haptic, openLink } from "core/telegram.js";

/* Per-palette swatch tuples (bg-0, accent, ink-hi) — used by the
   preview chip. Keep in sync with themes.css. */
const PALETTES = [
  { value: "auto",      label: "Auto (system)", swatch: null }, // dynamic
  { value: "dark",      label: "⚫ Dark",       swatch: ["#05060a", "#ff3b3b", "#f2f4f8"] },
  { value: "light",     label: "⚪ Light",      swatch: ["#f6f7fb", "#d92626", "#111318"] },
  { value: "sepia",     label: "📜 Sepia",      swatch: ["#f4ecd8", "#a0522d", "#3b2a17"] },
  { value: "dracula",   label: "🧛 Dracula",    swatch: ["#282a36", "#ff79c6", "#f8f8f2"] },
  { value: "midnight",  label: "🌃 Midnight",   swatch: ["#050a1a", "#4f8cff", "#eef2ff"] },
  { value: "amoled",    label: "🔲 AMOLED",     swatch: ["#000000", "#ff3a3a", "#ffffff"] },
  { value: "nord",      label: "❄️ Nord",       swatch: ["#2e3440", "#88c0d0", "#eceff4"] },
  { value: "solarized", label: "🌞 Solarized",  swatch: ["#002b36", "#b58900", "#fdf6e3"] },
  { value: "forest",    label: "🌲 Forest",     swatch: ["#0b1a10", "#4caf50", "#eaf5ec"] },
];

export async function render(root, { me }) {
  root.appendChild(section("Appearance", [
    selectRow("Interface mode", "theme_override", [
      { value: "auto",  label: "Match Telegram" },
      { value: "dark",  label: "Always Dark" },
      { value: "light", label: "Always Light" },
    ]),
    paletteRow(),                                   // v11.7: swatch dropdown
    toggleRow("Reduce motion",    "reduced_motion"),
  ]));
  root.appendChild(section("Interaction", [
    toggleRow("Haptic feedback",  "haptics_enabled"),
    toggleRow("Infinite scroll",  "infinite_scroll"),
    toggleRow("Show search hints", "show_hint_bar"),
  ]));
  root.appendChild(section("Data", [
    buttonRow("Reset preferences to defaults", () => {
      if (!confirm("Reset all local settings?")) return;
      prefs.reset();
      make("toast", { text: "Preferences reset", kind: "success" });
      root.innerHTML = "";
      render(root, { me });
    }, "danger"),
  ]));
  root.appendChild(section("Support", [ contactAdminRow() ]));

  // v11.8 (#8): What's new panel — renders admin-authored improvements.
  root.appendChild(improvementsSection());

  root.appendChild(h("div", {
    style: { textAlign: "center", padding: "16px",
             color: "var(--du-ink-lo)", fontSize: "12px" },
  }, "Doujinshi Universe · v0.1.0"));
}

/* ---------------------------------------------------------------
   v11.8 (#8) — What's new / Improvements panel
   Reads /api/improvements. Hidden when there's nothing to show.
   --------------------------------------------------------------- */
function improvementsSection() {
  const wrap = h("div", { class: "admin-section", style: { display: "none" }});
  wrap.appendChild(h("h3", {}, "🆕 What's new"));
  const list = h("div", {
    style: { display: "flex", flexDirection: "column", gap: "0" },
  });
  wrap.appendChild(list);

  (async () => {
    let items = [];
    try {
      const r = await fetch("/api/improvements?limit=30", { credentials: "include" });
      if (r.ok) items = (await r.json()).items || [];
    } catch (_) { items = []; }
    // Fallback: use core/api.js when direct fetch fails on the auth header.
    if (!items.length) {
      try {
        const mod = await import("core/api.js");
        const r = await mod.api.get("/api/improvements?limit=30");
        items = r.items || [];
      } catch (_) {}
    }
    if (!items.length) return;
    wrap.style.display = "";
    let first = true;
    for (const it of items) {
      const row = h("div", {
        style: {
          padding: "10px 2px",
          borderTop: first ? "0" : "1px solid var(--du-divider, rgba(255,255,255,0.06))",
        },
      });
      first = false;
      row.appendChild(h("div", {
        style: { fontSize: "13px", color: "var(--du-ink-hi)", lineHeight: "1.4",
                 whiteSpace: "pre-wrap", overflowWrap: "anywhere" },
      }, it.text || ""));
      const meta = [];
      if (it.author) meta.push(it.author);
      if (it.ts) meta.push(fmtImpDate(it.ts));
      if (meta.length) {
        row.appendChild(h("div", {
          style: { fontSize: "10px", color: "var(--du-ink-lo)",
                   marginTop: "4px", letterSpacing: "0.2px" },
        }, meta.join(" · ")));
      }
      list.appendChild(row);
    }
  })();

  return wrap;
}

function fmtImpDate(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric" });
  } catch (_) { return ""; }
}

function section(title, rows) {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, title));
  for (const r of rows) wrap.appendChild(r);
  return wrap;
}

function toggleRow(label, key) {
  const row = h("div", { class: "kv-row" });
  const btn = h("button", {
    class: "toggle",
    "aria-checked": prefs.get(key) ? "true" : "false",
  });
  btn.addEventListener("click", () => {
    const cur = btn.getAttribute("aria-checked") === "true";
    const next = !cur;
    btn.setAttribute("aria-checked", next ? "true" : "false");
    prefs.set(key, next);
    haptic("select");
  });
  row.append(h("span", { class: "k" }, label), btn);
  return row;
}

function selectRow(label, key, opts) {
  const row = h("div", { class: "kv-row" });
  const sel = h("select", {
    style: {
      background: "var(--du-bg-2)", color: "var(--du-ink-hi)",
      border: "1px solid var(--du-border)", borderRadius: "8px",
      padding: "6px 10px", fontSize: "13px",
    },
  }, ...opts.map(o => h("option", { value: o.value }, o.label)));
  sel.value = prefs.get(key);
  sel.addEventListener("change", () => {
    prefs.set(key, sel.value);
    haptic("select");
    if (key === "theme_override") {
      document.documentElement.dataset.theme =
        sel.value === "auto" ? (window.Telegram?.WebApp?.colorScheme || "dark") : sel.value;
    }
  });
  row.append(h("span", { class: "k" }, label), sel);
  return row;
}

/* ---------------------------------------------------------------
   paletteRow — v11.7 custom picker with 3-color swatch preview.
   The visible chip shows the currently active palette's swatch;
   the dropdown lists all palettes with their own mini swatches.
   Auto option's swatch adapts to whatever resolvePalette() returns.
   --------------------------------------------------------------- */
function paletteRow() {
  const row  = h("div", { class: "kv-row", style: { alignItems: "center" } });
  const cur  = prefs.get("background_theme") || "auto";

  // Chip button — the visible current value
  const chip = h("button", {
    class: "btn secondary",
    style: {
      display: "inline-flex", alignItems: "center", gap: "8px",
      padding: "6px 10px", fontSize: "13px", borderRadius: "10px",
    },
    "aria-haspopup": "listbox",
    "aria-expanded": "false",
  });

  // Popover (hidden until opened)
  const pop = h("div", {
    class: "palette-pop",
    style: {
      position: "absolute", zIndex: "1000",
      right: "0", marginTop: "6px",
      minWidth: "240px", maxHeight: "360px", overflowY: "auto",
      background: "var(--du-bg-1)",
      border: "1px solid var(--du-border-strong)",
      borderRadius: "14px",
      boxShadow: "var(--du-shadow-lg, 0 12px 32px rgba(0,0,0,0.45))",
      padding: "8px",
      display: "none",
    },
    role: "listbox",
  });

  // v11.8 (#9): single BIG circle per palette (like the reference
  // screenshot) — background fill with an accent-coloured ring so even
  // near-white / near-black palettes are unmistakable at a glance.
  function swatchDots(sw) {
    const bg = sw[0], accent = sw[1];
    return h("span", {
      style: {
        width: "18px", height: "18px", borderRadius: "50%",
        background: bg,
        border: `2.5px solid ${accent}`,
        boxShadow: "0 0 0 1px rgba(0,0,0,0.15)",
        display: "inline-block", flex: "0 0 auto",
      },
    });
  }

  function paletteFor(value) {
    return PALETTES.find(p => p.value === value) || PALETTES[0];
  }

  function currentSwatch(value) {
    const p = paletteFor(value);
    if (p.swatch) return p.swatch;
    // "auto" → mirror the resolved palette's swatch
    const resolved = paletteFor(resolvePalette("auto")) || paletteFor("dark");
    return resolved.swatch;
  }

  function renderChip(value) {
    chip.innerHTML = "";
    const p = paletteFor(value);
    chip.appendChild(swatchDots(currentSwatch(value)));
    let text = p.label;
    if (value === "auto") {
      const r = resolvePalette("auto");
      text += ` (${r})`;
    }
    chip.appendChild(h("span", {}, text));
    chip.appendChild(h("span", { style: { opacity: "0.5" } }, "▾"));
  }

  function renderList() {
    pop.innerHTML = "";
    const active = prefs.get("background_theme") || "auto";
    for (const p of PALETTES) {
      const isActive = (p.value === active);
      const sw = currentSwatch(p.value);
      const item = h("button", {
        class: "palette-item",
        role: "option",
        "aria-selected": isActive ? "true" : "false",
        style: {
          display: "flex", alignItems: "center", gap: "12px",
          width: "100%", padding: "10px 12px",
          background: isActive ? "var(--du-bg-2)" : "transparent",
          color: isActive ? "var(--du-accent)" : "var(--du-ink-hi)",
          fontWeight: isActive ? "600" : "500",
          border: "0", borderRadius: "8px",
          cursor: "pointer", textAlign: "left", fontSize: "14px",
        },
      });
      item.appendChild(swatchDots(sw));
      item.appendChild(h("span", { style: { flex: "1" } }, p.label));
      if (isActive) item.appendChild(h("span", { style: { opacity: "0.85" } }, "✓"));
      item.addEventListener("click", () => {
        prefs.set("background_theme", p.value);
        haptic("select");
        renderChip(p.value);
        close();
      });
      pop.appendChild(item);
    }
  }

  function open()  { pop.style.display = "block"; chip.setAttribute("aria-expanded", "true");  renderList(); }
  function close() { pop.style.display = "none";  chip.setAttribute("aria-expanded", "false"); }

  chip.addEventListener("click", (e) => {
    e.stopPropagation();
    (pop.style.display === "block") ? close() : open();
  });
  // Close on outside click
  document.addEventListener("click", (e) => {
    if (!wrap.contains(e.target)) close();
  });

  const wrap = h("div", {
    style: { position: "relative", display: "inline-block" },
  }, chip, pop);

  renderChip(cur);

  row.append(h("span", { class: "k" }, "Palette"), wrap);
  return row;
}

function buttonRow(label, onClick, kind = "secondary") {
  return h("button", {
    class: "btn " + kind + " block",
    style: { marginTop: "8px" },
    onclick: onClick,
  }, label);
}

function contactAdminRow() {
  return h("div", { style: { marginTop: "8px" } },
    h("button", {
      class: "btn primary block btn-stylish",
      onclick: () => {
        haptic("medium");
        try { openLink("https://t.me/reportupdatesbot"); }
        catch (e) { window.open("https://t.me/reportupdatesbot", "_blank"); }
      },
    }, "💬 Contact Admin"),
    h("div", {
      style: { color: "var(--du-ink-lo)", fontSize: "11px",
               margin: "6px 2px 0", lineHeight: "1.4" },
    }, "Report a bug, request a feature, or ask the admin anything — "
     + "opens @reportupdatesbot in Telegram."),
  );
}
