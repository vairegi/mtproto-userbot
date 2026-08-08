/*
  prefs.js — Client-side user preferences (localStorage)  [v11.7]

  v11.7 changes:
    * DEFAULTS.background_theme = "auto"  — new "auto" option that follows
      the OS-level color-scheme (prefers-color-scheme) + optional Telegram
      colorScheme hint. Concrete palette is resolved at apply() time.
    * apply() now writes a *resolved* palette name to <html data-bg-theme>
      whenever background_theme = "auto", and re-runs on `matchMedia`
      change so the app flips instantly with the OS.
    * v11.6's "ember" alias of "dark" preserved for back-compat.
*/

const KEY = "du_prefs_v1";

const DEFAULTS = {
  theme_override: "auto",       // "auto" | "dark" | "light"
  // v11.7: palette theme.
  //   "auto"      — follow OS (prefers-color-scheme) → picks dark or light
  //   "dark"      — force dark
  //   "light"     — force light
  //   "sepia" | "dracula" | "midnight" | "amoled" |
  //   "nord"  | "solarized" | "forest"
  //   "ember"     — legacy alias of dark (kept so old localStorage resolves)
  background_theme: "auto",
  haptics_enabled: true,
  infinite_scroll: true,
  reduced_motion: false,
  show_hint_bar: true,
};

function load() {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return { ...DEFAULTS };
    const parsed = JSON.parse(raw);
    return { ...DEFAULTS, ...parsed };
  } catch (_) {
    return { ...DEFAULTS };
  }
}

let cache = load();

function persist() {
  try { localStorage.setItem(KEY, JSON.stringify(cache)); } catch (_) {}
}

// v11.7: resolve "auto" -> concrete palette by inspecting OS + Telegram.
// Exported so settings.js can display the resolved value in the label.
export function resolvePalette(name) {
  if (name && name !== "auto") return name === "ember" ? "dark" : name;
  // 1. Telegram's own colorScheme (matches the user's Telegram theme).
  const tgScheme = window.Telegram?.WebApp?.colorScheme;
  if (tgScheme === "light") return "light";
  if (tgScheme === "dark")  return "dark";
  // 2. Fall back to the OS media query.
  if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) {
    return "light";
  }
  return "dark";
}

export const prefs = {
  get(key)         { return cache[key]; },
  set(key, value)  { cache[key] = value; persist(); apply(); },
  all()            { return { ...cache }; },
  reset()          { cache = { ...DEFAULTS }; persist(); apply(); },
  // v11.7: exposed so settings.js can show "Auto (currently: Light)".
  resolveBgTheme() { return resolvePalette(cache.background_theme); },
};

function apply() {
  const override = cache.theme_override;
  if (override === "auto") delete document.documentElement.dataset.themeForced;
  else document.documentElement.dataset.theme = override;
  document.documentElement.dataset.reducedMotion = cache.reduced_motion ? "1" : "0";
  // v11.7: resolve "auto" to a real palette before writing to the DOM so
  // themes.css always finds a matching [data-bg-theme=X] block.
  const resolved = resolvePalette(cache.background_theme);
  document.documentElement.dataset.bgTheme = resolved;
}
apply();

// v11.7: when the user is on "auto", re-apply whenever the OS toggles
// between light and dark (e.g. iOS Night Shift, macOS sunset). Users on a
// forced palette are unaffected — apply() only touches the DOM.
if (window.matchMedia) {
  const mq = window.matchMedia("(prefers-color-scheme: light)");
  const rerun = () => { if (cache.background_theme === "auto") apply(); };
  // Chrome / Firefox / Safari 14+
  if (mq.addEventListener) mq.addEventListener("change", rerun);
  else if (mq.addListener) mq.addListener(rerun);  // legacy Safari
}
