/*
  toast.js — Ephemeral notifications

  Usage:
    import { make } from "core/components.js";
    make("toast", { text: "Queued ✓", kind: "success" });
*/

import { register, h } from "core/components.js";
import { haptic } from "core/telegram.js";

register("toast", ({ text, kind = "", duration = 2400 }) => {
  const root = document.getElementById("toast-root");
  const el = h("div", { class: "toast " + kind }, text);
  root.appendChild(el);
  if (kind === "success") haptic("success");
  else if (kind === "error") haptic("error");
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transform = "translateY(8px)";
    setTimeout(() => el.remove(), 220);
  }, duration);
  return el;
});
