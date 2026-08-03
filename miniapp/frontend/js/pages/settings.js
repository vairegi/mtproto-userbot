/*
  pages/settings.js — Client-side preferences

  All settings here are local-only (localStorage via core/prefs.js).  For
  admin-controlled server settings, see pages/admin.js.

  To add a new toggle/preference:
    1. Add default to core/prefs.js DEFAULTS.
    2. Add a row here via toggleRow() or selectRow().
  That's it.
*/

import { h, make } from "core/components.js";
import { prefs } from "core/prefs.js";
import { haptic } from "core/telegram.js";

export async function render(root, { me }) {
  root.appendChild(section("Appearance", [
    selectRow("Theme", "theme_override", [
      { value: "auto",  label: "Match Telegram" },
      { value: "dark",  label: "Always Dark" },
      { value: "light", label: "Always Light" },
    ]),
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
      // Re-render this page to reflect defaults
      root.innerHTML = "";
      render(root, { me });
    }, "danger"),
  ]));

  root.appendChild(h("div", {
    style: { textAlign: "center", padding: "16px",
             color: "var(--du-ink-lo)", fontSize: "12px" },
  }, "Doujinshi Universe · v0.1.0"));
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
    // Theme override needs to re-apply immediately.
    if (key === "theme_override") {
      document.documentElement.dataset.theme =
        sel.value === "auto" ? (window.Telegram?.WebApp?.colorScheme || "dark") : sel.value;
    }
  });
  row.append(h("span", { class: "k" }, label), sel);
  return row;
}

function buttonRow(label, onClick, kind = "secondary") {
  return h("button", {
    class: "btn " + kind + " block",
    style: { marginTop: "8px" },
    onclick: onClick,
  }, label);
}
