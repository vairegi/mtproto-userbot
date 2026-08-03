/*
  chip.js — Filter chip / toggleable pill

  Props:
    label     text shown
    active    initial state
    onChange  (newActive) => void
*/

import { register, h } from "core/components.js";
import { haptic } from "core/telegram.js";

register("chip", ({ label, active = false, onChange }) => {
  const el = h("button", {
    class: "chip",
    "aria-pressed": active ? "true" : "false",
    type: "button",
  }, label);
  el.addEventListener("click", () => {
    const cur = el.getAttribute("aria-pressed") === "true";
    el.setAttribute("aria-pressed", cur ? "false" : "true");
    haptic("select");
    onChange && onChange(!cur);
  });
  return el;
});
