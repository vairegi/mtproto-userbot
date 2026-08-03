/*
  skeleton.js — Loading placeholder blocks

  Usage:
    import { make } from "core/components.js";
    container.appendChild(make("skeleton", { variant: "card-grid", count: 6 }));
*/

import { register, h } from "core/components.js";

register("skeleton", ({ variant = "block", count = 1, height = 120 }) => {
  if (variant === "card-grid") {
    const grid = h("div", { class: "card-grid" });
    for (let i = 0; i < count; i++) {
      grid.appendChild(h("div", { class: "gallery-card" },
        h("div", { class: "cover skeleton", style: { aspectRatio: "2 / 3" } }),
        h("div", { class: "meta" },
          h("div", { class: "skeleton", style: { height: "14px", width: "80%" } }),
          h("div", { class: "skeleton", style: { height: "10px", width: "40%", marginTop: "6px" } }),
        ),
      ));
    }
    return grid;
  }
  return h("div", { class: "skeleton", style: { height: height + "px" } });
});
