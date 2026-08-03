/*
  telegram.js — Thin wrapper around window.Telegram.WebApp

  Every Telegram API call goes through here so we can:
    - Fake it in a browser dev environment (no WebApp global)
    - Swap out for a newer SDK version without changing every page
    - Centralize haptic patterns

  Add a new Telegram feature: add a method here, use it from pages.
*/

import { prefs } from "core/prefs.js";

const tg = (typeof window !== "undefined" && window.Telegram && window.Telegram.WebApp) || null;

export const isTelegram = !!tg;

// ---- Boot: expand, sync theme, register events -------------------------
export function bootTelegram({ onThemeChange } = {}) {
  if (!tg) {
    // Dev mode: apply a sane default theme via data-attr and return.
    document.documentElement.dataset.theme = "dark";
    return { initData: "", user: null, colorScheme: "dark" };
  }
  tg.ready();
  tg.expand();
  applyThemeParams(tg.themeParams, tg.colorScheme);
  tg.onEvent("themeChanged", () => {
    applyThemeParams(tg.themeParams, tg.colorScheme);
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

function applyThemeParams(params, scheme) {
  // User's manual override wins over Telegram's colorScheme.
  const override = prefs.get("theme_override");
  const effective = (override && override !== "auto") ? override : (scheme || "dark");
  document.documentElement.dataset.theme = effective;
  if (!params) return;
  const map = {
    bg_color:               "--tg-bg",
    text_color:             "--tg-text",
    hint_color:             "--tg-hint",
    link_color:             "--tg-link",
    button_color:           "--tg-button",
    button_text_color:      "--tg-button-text",
    secondary_bg_color:     "--tg-secondary-bg",
    header_bg_color:        "--tg-header-bg",
    accent_text_color:      "--tg-accent-text",
    section_bg_color:       "--tg-section-bg",
    section_header_text_color: "--tg-section-header",
    subtitle_text_color:    "--tg-subtitle",
    destructive_text_color: "--tg-destructive",
  };
  const root = document.documentElement.style;
  for (const [k, v] of Object.entries(map)) {
    if (params[k]) root.setProperty(v, params[k]);
  }
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
