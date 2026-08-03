/*
  sheet.js — Bottom sheet modal

  Usage:
    import { make } from "core/components.js";
    const sheet = make("sheet", { title: "Detail", body: someNode });
    sheet.open();
    // ... later:
    sheet.close();
*/

import { register, h } from "core/components.js";
import { haptic } from "core/telegram.js";
import { push as pushBack } from "core/back-stack.js";

register("sheet", ({ title, body, actions }) => {
  const root = document.getElementById("sheet-root");

  const backdrop = h("div", { class: "sheet-backdrop" });
  const sheetEl  = h("div", { class: "sheet", role: "dialog", "aria-modal": "true" },
    h("div", { class: "sheet-handle" }),
    title ? h("h2", { style: { margin: "0 0 12px", fontSize: "17px" } }, title) : null,
    body || null,
    actions && actions.length
      ? h("div", { style: { display: "flex", gap: "8px", marginTop: "16px" } },
          ...actions.map(a => {
            const btn = h("button", {
              class: "btn " + (a.kind || "secondary") + " " + (a.block ? "block" : ""),
              onclick: () => { haptic(a.haptic || "light"); a.onClick && a.onClick(api); },
            }, a.label);
            return btn;
          }))
      : null,
  );

  let popBack = null;
  const api = {
    open() {
      root.appendChild(backdrop);
      root.appendChild(sheetEl);
      requestAnimationFrame(() => {
        backdrop.classList.add("open");
        sheetEl.classList.add("open");
      });
      backdrop.addEventListener("click", api.close, { once: true });
      // Register with the global back-stack so Telegram's back button
      // dismisses this sheet before exiting the app.
      popBack = pushBack(() => api.close());
    },
    close() {
      if (popBack) { popBack(); popBack = null; }
      backdrop.classList.remove("open");
      sheetEl.classList.remove("open");
      setTimeout(() => {
        backdrop.remove();
        sheetEl.remove();
      }, 240);
    },
    el: sheetEl,
  };
  return api;
});
