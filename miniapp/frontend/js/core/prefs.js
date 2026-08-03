/*
  prefs.js — Client-side user preferences (localStorage)

  Keeps small, purely-UI settings on the device: haptics on/off, forced
  theme override, infinite-scroll on/off, etc. Non-critical stuff that
  doesn't need to survive a re-install and isn't worth a MongoDB round-trip.

  For per-user data that MUST persist across devices (bookmarks, rate-limit
  quota, ban status, etc.) use the backend + Mongo — that's what
  /api/bookmarks + /api/admin/users are for.

  Add a new preference:
    1. Add a default in DEFAULTS below.
    2. Read via prefs.get("myKey").
    3. Write via prefs.set("myKey", value).
    4. Optionally expose a toggle on the Settings sub-page.
*/

const KEY = "du_prefs_v1";

const DEFAULTS = {
  theme_override: "auto",       // "auto" | "dark" | "light"
  haptics_enabled: true,
  infinite_scroll: true,
  reduced_motion: false,
  show_hint_bar: true,           // the "Try: tag:vanilla" hint under search
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

export const prefs = {
  get(key)         { return cache[key]; },
  set(key, value)  { cache[key] = value; persist(); apply(); },
  all()            { return { ...cache }; },
  reset()          { cache = { ...DEFAULTS }; persist(); apply(); },
};

function apply() {
  const override = cache.theme_override;
  if (override === "auto") delete document.documentElement.dataset.themeForced;
  else document.documentElement.dataset.theme = override;
  document.documentElement.dataset.reducedMotion = cache.reduced_motion ? "1" : "0";
}
apply();
