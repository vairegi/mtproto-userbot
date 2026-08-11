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
    paletteRow(),                                   // v12.17: flat list
    toggleRow("Reset theme on close", "reset_theme_on_close"),
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
   paletteRow — v12.17 flat vertical picker, matching the reference
   screenshot: colored dot + label per row, active row highlighted in
   accent color with a ✓ on the right. No chip+popover. The "auto"
   option mirrors whatever resolvePalette() returns.
   --------------------------------------------------------------- */
function paletteRow() {
  const wrap = h("div", { class: "kv-row", style: { flexDirection: "column", alignItems: "stretch" } });
  wrap.appendChild(h("span", { class: "k" }, "Palette"));
  const list = h("div", {
    style: { display: "flex", flexDirection: "column", gap: "0" },
    role: "listbox",
  });
  wrap.appendChild(list);

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
    const resolved = paletteFor(resolvePalette("auto")) || paletteFor("dark");
    return resolved.swatch;
  }

  function renderList() {
    list.innerHTML = "";
    const active = prefs.get("background_theme") || "auto";
    for (const p of PALETTES) {
      const isActive = (p.value === active);
      const sw = currentSwatch(p.value);
      let label = p.label;
      if (p.value === "auto") label += ` (${resolvePalette("auto")})`;
      const item = h("button", {
        class: "palette-item",
        role: "option",
        "aria-selected": isActive ? "true" : "false",
        style: {
          display: "flex", alignItems: "center", gap: "12px",
          width: "100%", padding: "12px 10px",
          background: "transparent",
          color: isActive ? "var(--du-accent)" : "var(--du-ink-hi)",
          fontWeight: isActive ? "600" : "500",
          border: "0", borderRadius: "8px",
          cursor: "pointer", textAlign: "left", fontSize: "15px",
          borderTop: isActive ? "0" : "1px solid var(--du-divider, rgba(255,255,255,0.06))",
        },
      });
      item.appendChild(swatchDots(sw));
      item.appendChild(h("span", { style: { flex: "1" } }, label));
      if (isActive) item.appendChild(h("span", { style: { opacity: "0.85" } }, "✓"));
      item.addEventListener("click", () => {
        prefs.set("background_theme", p.value);
        haptic("select");
        renderList();  // repaint the whole list so the ✓ moves
      });
      list.appendChild(item);
    }
  }

  renderList();
  return wrap;
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
