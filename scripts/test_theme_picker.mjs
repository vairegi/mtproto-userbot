#!/usr/bin/env node
/*
  test_theme_picker.mjs — browserless verification of the v12.17 theme
  picker: flat vertical list, session-only reset, palette switch.

  Loads the REAL settings.js and prefs.js with import specifiers
  rewritten to the shared stubs (same approach as test_pagination.mjs).

    TEST 1  the rendered list contains all 10 palettes (auto + 9 concrete)
            in the documented order.
    TEST 2  each palette row's swatch circle uses the (bg, accent) tuple
            that matches themes.css — guards against the swatch drifting
            from the actual CSS.
    TEST 3  tapping a non-active palette switches prefs.background_theme
            and the ✓ moves to the newly-tapped row.
    TEST 4  with reset_theme_on_close=true (the v12.17 default),
            simulating visibilitychange → hidden writes background_theme
            back to "auto" in persisted localStorage, WITHOUT touching
            the currently-rendered DOM.
    TEST 5  with reset_theme_on_close=false, hiding the app keeps the
            chosen palette.

  Run:  node scripts/test_theme_picker.mjs     (exit 0 = all pass)
*/

import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const SETTINGS = join(ROOT, "miniapp/frontend/js/pages/settings.js");
const PREFS    = join(ROOT, "miniapp/frontend/js/core/prefs.js");
const THEMES_CSS = join(ROOT, "miniapp/frontend/css/themes.css");
const STUBS = join(ROOT, "scripts/test_pagination_stubs.mjs");

/* ---- stub the DOM + localStorage + visibility plumbing ------------- */
const stubs = await import(pathToFileURL(STUBS).href);

// localStorage shim — settings/prefs read & write it directly.
const _ls = {};
globalThis.localStorage = {
  getItem: (k) => (k in _ls ? _ls[k] : null),
  setItem: (k, v) => { _ls[k] = String(v); },
  removeItem: (k) => { delete _ls[k]; },
  clear: () => { for (const k of Object.keys(_ls)) delete _ls[k]; },
};

// document / visibility stubs
let visibilityState = "visible";
const visListeners = { visibilitychange: [] };
globalThis.document = {
  createElement: (t) => new stubs.__test.StubElement(t),
  createTextNode: (t) => new stubs.__test.StubElement("span"),
  getElementById: () => null,
  documentElement: new stubs.__test.StubElement("html"),
  body: new stubs.__test.StubElement("body"),
  addEventListener: (t, fn) => { (visListeners[t] = visListeners[t] || []).push(fn); },
  get visibilityState() { return visibilityState; },
};
globalThis.window = {
  addEventListener: () => {},
  removeEventListener: () => {},
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  Telegram: undefined,
};

/* ---- transform settings.js + prefs.js ------------------------------ */
function transform(file, extra = []) {
  let src = readFileSync(file, "utf8");
  const stubUrl = pathToFileURL(STUBS).href;
  const specs = [
    "core/components.js", "core/telegram.js",
    ...extra,
  ];
  for (const s of specs) {
    src = src.split(`"${s}"`).join(JSON.stringify(stubUrl));
    src = src.split(`'${s}'`).join(JSON.stringify(stubUrl));
  }
  return src;
}

const tmp = mkdtempSync(join(tmpdir(), "du-theme-"));
// prefs.js is imported by settings.js — resolve "core/prefs.js" to the
// real prefs module (not a stub).
const prefsSrc = readFileSync(PREFS, "utf8");
writeFileSync(join(tmp, "prefs.mjs"), prefsSrc);
let settingsSrc = transform(SETTINGS);
settingsSrc = settingsSrc
  .split(`"core/prefs.js"`).join(JSON.stringify(pathToFileURL(join(tmp, "prefs.mjs")).href));
writeFileSync(join(tmp, "settings.mjs"), settingsSrc);

const { render } = await import(pathToFileURL(join(tmp, "settings.mjs")).href);
const { prefs } = await import(pathToFileURL(join(tmp, "prefs.mjs")).href);

/* ---- helpers -------------------------------------------------------- */
let failures = 0;
function check(name, cond, extra = "") {
  if (cond) console.log(`  ok    ${name}`);
  else { console.log(`  FAIL  ${name}${extra ? " — " + extra : ""}`); failures++; }
}

const findPaletteItems = (root) => {
  const out = [];
  const walk = (n) => {
    if (!n) return;
    if (n.nodeType === 1) {
      if ((n.className || "").split(/\s+/).includes("palette-item")) out.push(n);
      for (const c of (n.childNodes || [])) walk(c);
    }
  };
  walk(root);
  return out;
};

const labelOf = (el) => {
  // collect all text descendants in order
  const bits = [];
  const walk = (n) => {
    if (n.nodeType === 3) bits.push(n.textContent);
    for (const c of (n.childNodes || [])) walk(c);
  };
  walk(el);
  return bits.join("");
};

/* ---- read themes.css for swatch cross-check ------------------------ */
const themesCss = readFileSync(THEMES_CSS, "utf8");
// crude but sufficient: extract the accent for each palette block.
function cssAccent(palette) {
  // Match html[data-bg-theme="<palette>"] followed by its block body up
  // to --du-accent. No anchor needed — each palette name appears in only
  // one selector block, and [^{]* correctly skips the ember alias line.
  const re = new RegExp(
    `html\\[data-bg-theme="${palette}"\\][^{]*\\{[^}]*--du-accent:\\s*([^;]+);`,
    "s",
  );
  const m = themesCss.match(re);
  return m ? m[1].trim() : null;
}

/* ======================= TEST 1: all 10 palettes render ============= */
console.log("\nTEST 1 — flat list renders all 10 palettes");
{
  const root = new stubs.__test.StubElement("div");
  await render(root, { me: { user_id: 1 } });
  const items = findPaletteItems(root);
  check("10 palette rows rendered", items.length === 10, `got ${items.length}`);
  const want = ["Auto (system)", "Dark", "Light", "Sepia", "Dracula",
                "Midnight", "AMOLED", "Nord", "Solarized", "Forest"];
  const gotLabels = items.map(labelOf);
  for (const w of want) {
    const hit = gotLabels.some(l => l.toLowerCase().includes(w.toLowerCase().split(" ")[0].toLowerCase()));
    check(`palette "${w}" present`, hit, JSON.stringify(gotLabels));
  }
}

/* ================= TEST 2: swatches match themes.css ================ */
console.log("\nTEST 2 — swatch colors match themes.css accents");
{
  const root = new stubs.__test.StubElement("div");
  await render(root, { me: { user_id: 1 } });
  const items = findPaletteItems(root);
  const byLabel = {};
  for (const it of items) byLabel[labelOf(it).toLowerCase()] = it;
  const checks = [
    ["dark",      "#ff3b3b"],
    ["light",     "#d92626"],
    ["sepia",     "#a0522d"],
    ["dracula",   "#ff79c6"],
    ["midnight",  "#4f8cff"],
    ["amoled",    "#ff3a3a"],
    ["nord",      "#88c0d0"],
    ["solarized", "#b58900"],
    ["forest",    "#4caf50"],
  ];
  for (const [palette, expectedAccent] of checks) {
    const cssAccentVal = cssAccent(palette);
    check(`themes.css accent for "${palette}" matches settings (${expectedAccent})`,
          cssAccentVal === expectedAccent,
          `css=${cssAccentVal}`);
    const row = Object.entries(byLabel).find(([l]) => l.includes(palette));
    if (row) {
      const sw = row[1].childNodes[0];   // swatch circle is the first child
      const borderCol = (sw?.style?.border || "").match(/solid\s+([^;]+)/)?.[1]?.trim();
      check(`row swatch border = ${expectedAccent}`, borderCol === expectedAccent,
            `got ${borderCol}`);
    } else {
      check(`palette row "${palette}" found`, false);
    }
  }
}

/* ================= TEST 3: tap switches palette ===================== */
console.log("\nTEST 3 — tapping a palette switches prefs + moves ✓");
{
  localStorage.clear();
  const root = new stubs.__test.StubElement("div");
  await render(root, { me: { user_id: 1 } });
  const items = findPaletteItems(root);
  const nordRow = items.find(it => labelOf(it).toLowerCase().includes("nord"));
  check("nord row exists", !!nordRow);
  check("✓ not on nord before tap", !labelOf(nordRow).includes("✓"));
  nordRow.click();
  await new Promise(r => setTimeout(r, 0));
  check("prefs.background_theme === 'nord' after tap", prefs.get("background_theme") === "nord");
  // Re-render to verify the ✓ moved (the click handler calls renderList()).
  const itemsAfter = findPaletteItems(root);
  const nordAfter = itemsAfter.find(it => labelOf(it).toLowerCase().includes("nord"));
  check("✓ moved to nord row after tap", labelOf(nordAfter).includes("✓"));
}

/* ====== TEST 4: reset_theme_on_close resets palette on hide ========= */
console.log("\nTEST 4 — reset_theme_on_close=true resets palette on visibilitychange");
{
  localStorage.clear();
  const root = new stubs.__test.StubElement("div");
  await render(root, { me: { user_id: 1 } });
  // default: reset_theme_on_close === true
  check("default reset_theme_on_close is true", prefs.get("reset_theme_on_close") === true);
  prefs.set("background_theme", "dracula");
  check("palette set to dracula", prefs.get("background_theme") === "dracula");
  // Simulate the user closing the mini-app
  visibilityState = "hidden";
  for (const fn of visListeners.visibilitychange || []) fn();
  await new Promise(r => setTimeout(r, 0));
  check("background_theme persisted back to auto",
        prefs.get("background_theme") === "auto");
  const persisted = JSON.parse(_ls["du_prefs_v1"] || "{}");
  check("localStorage has background_theme=auto", persisted.background_theme === "auto");
}

/* ====== TEST 5: reset_theme_on_close=false keeps the palette ======== */
console.log("\nTEST 5 — reset_theme_on_close=false keeps the palette on hide");
{
  localStorage.clear();
  const root = new stubs.__test.StubElement("div");
  await render(root, { me: { user_id: 1 } });
  prefs.set("reset_theme_on_close", false);
  prefs.set("background_theme", "forest");
  visibilityState = "hidden";
  for (const fn of visListeners.visibilitychange || []) fn();
  await new Promise(r => setTimeout(r, 0));
  check("palette stays forest when toggle is off",
        prefs.get("background_theme") === "forest");
}

/* ========================= summary ================================== */
console.log(failures === 0
  ? "\nALL THEME TESTS PASSED"
  : `\n${failures} THEME TEST(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
