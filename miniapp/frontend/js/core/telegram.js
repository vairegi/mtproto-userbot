/*
  telegram.js — Thin wrapper around window.Telegram.WebApp

  Every Telegram API call goes through here so we can:
    - Fake it in a browser dev environment (no WebApp global)
    - Swap out for a newer SDK version without changing every page
    - Centralize haptic patterns

  v0.3 change — THEME:
    We no longer copy Telegram's surface colors (bg_color, secondary_bg_color,
    header_bg_color, section_bg_color) into CSS variables. On Telegram's light
    theme those are white, which used to turn the whole Mini App white.
    Only ACCENT colors are borrowed. Surfaces live in theme.css.

    We also actively paint Telegram's own header/bottom chrome dark so the
    native bars match the app instead of flashing white.
*/

import { prefs } from "core/prefs.js";

const tg = (typeof window !== "undefined" && window.Telegram && window.Telegram.WebApp) || null;

export const isTelegram = !!tg;

// Must match --du-bg-0 in theme.css.
const APP_BG = "#0b0614";

// ---- Boot: expand, sync theme, register events -------------------------
export function bootTelegram({ onThemeChange } = {}) {
  if (!tg) {
    // Dev mode: apply a sane default theme via data-attr and return.
    document.documentElement.dataset.theme = resolveTheme("dark");
    return { initData: "", user: null, colorScheme: "dark" };
  }
  tg.ready();
  tg.expand();
  applyThemeParams(tg.themeParams, tg.colorScheme);
  paintNativeChrome();
  tg.onEvent("themeChanged", () => {
    applyThemeParams(tg.themeParams, tg.colorScheme);
    paintNativeChrome();
    onThemeChange && onThemeChange(tg.colorScheme);
  });
  return {
    initData: tg.initData || "",
    user: (tg.initDataUnsafe && tg.initDataUnsafe.user) || null,
    colorScheme: tg.colorScheme || "dark",
    version: tg.version,
    platform: tg.platform,
  };
}

function resolveTheme(scheme) {
  // User's manual override wins over Telegram's colorScheme.
  // Default is DARK regardless of Telegram — the artwork backdrop needs it.
  const override = prefs.get("theme_override");
  if (override === "light" || override === "dark") return override;
  return "dark";
}

/** Paint Telegram's native header + bottom bar to match the app shell. */
function paintNativeChrome() {
  const light = document.documentElement.dataset.theme === "light";
  const color = light ? "#fafafa" : APP_BG;
  try { tg.setHeaderColor && tg.setHeaderColor(color); } catch (_) {}
  try { tg.setBackgroundColor && tg.setBackgroundColor(color); } catch (_) {}
  try { tg.setBottomBarColor && tg.setBottomBarColor(color); } catch (_) {}
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", color);
}

function applyThemeParams(params, scheme) {
  document.documentElement.dataset.theme = resolveTheme(scheme);
  if (!params) return;

  // ACCENTS ONLY. Surface colors are intentionally NOT in this map — adding
  // bg_color / secondary_bg_color / header_bg_color back here will make the
  // app go white again on Telegram's light theme.
  const map = {
    link_color:                "--tg-link",
    button_color:              "--tg-button",
    button_text_color:         "--tg-button-text",
    accent_text_color:         "--tg-accent-text",
    section_header_text_color: "--tg-section-header",
    destructive_text_color:    "--tg-destructive",
  };
  const root = document.documentElement.style;
  for (const [k, v] of Object.entries(map)) {
    if (params[k]) root.setProperty(v, params[k]);
  }
}

/** Called by the Settings page after the user flips the theme override. */
export function refreshTheme() {
  applyThemeParams(tg ? tg.themeParams : null, tg ? tg.colorScheme : "dark");
  if (tg) paintNativeChrome();
}

// ---- Haptics -----------------------------------------------------------
export function haptic(kind = "light") {
  if (!prefs.get("haptics_enabled")) return;
  if (!tg || !tg.HapticFeedback) return;
  try {
    if (["success", "error", "warning"].includes(kind)) {
      tg.HapticFeedback.notificationOccurred(kind);
    } else if (kind === "select") {
      tg.HapticFeedback.selectionChanged();
    } else {
      tg.HapticFeedback.impactOccurred(kind); // light/medium/heavy/rigid/soft
    }
  } catch (_) { /* ignore */ }
}

// ---- Back button -------------------------------------------------------
export function showBackButton(handler) {
  if (!tg || !tg.BackButton) return () => {};
  tg.BackButton.show();
  const wrapped = () => handler && handler();
  tg.BackButton.onClick(wrapped);
  return () => {
    tg.BackButton.offClick(wrapped);
    tg.BackButton.hide();
  };
}

// ---- Main button -------------------------------------------------------
export function showMainButton({ text, color, textColor, onClick }) {
  if (!tg || !tg.MainButton) return () => {};
  const mb = tg.MainButton;
  if (text) mb.setText(text);
  if (color) mb.color = color;
  if (textColor) mb.textColor = textColor;
  mb.show();
  const wrapped = () => onClick && onClick();
  mb.onClick(wrapped);
  return () => { mb.offClick(wrapped); mb.hide(); };
}

// ---- Misc --------------------------------------------------------------
export function openLink(url, opts) {
  if (tg && tg.openLink) return tg.openLink(url, opts);
  window.open(url, "_blank");
}
export function openTelegramLink(url) {
  if (tg && tg.openTelegramLink) return tg.openTelegramLink(url);
  window.open(url, "_blank");
}
export function closeApp() { tg && tg.close && tg.close(); }
export function getInitData() { return tg ? tg.initData || "" : ""; }
