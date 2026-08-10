/*
  card.js — Gallery card component

  Props:
    id          nhentai gallery id
    title       cleaned title (full, kept for the detail sheet / tooltip)
    title_en_clean  v12.10 (#8): grid-only cleaned English title; falls
                back to `title` when empty/missing — never renders empty
    cover       cover image URL
    pages       page count
    tags        [{ name, type }]  optional
    onOpen      click handler for the whole card
    actions     [{ label, onClick, kind }]  optional action buttons under cover

  This component renders the card and delegates the ACTIONS list to the
  card-actions plugin.  To change which buttons appear on cards, edit
  plugins/card-actions.js — NOT this file.
*/

import { register, h } from "core/components.js";
import { haptic } from "core/telegram.js";

register("card", (props) => {
  const { id, title, cover, pages: pageCount, badge, onOpen } = props;
  // v12.10 (#8): grid shows the cleaned English title when present.
  const gridTitle = (props.title_en_clean || "").trim() || title || "";

  const card = h("article", {
    class: "gallery-card",
    dataset: { galleryId: id },
    onclick: () => { haptic("light"); onOpen && onOpen(props); },
  },
    cover
      ? (() => {
          // v12.10 (#9, option A): landscape covers get .wide-card so the
          // card spans the full grid row (see components.css). The probe
          // runs on load AND immediately for cached images (complete flag),
          // so the class lands before first paint whenever possible.
          const img = h("img", { class: "cover", src: cover, loading: "lazy", alt: title || "cover" });
          const probe = () => {
            if (img.naturalWidth > 0 && img.naturalWidth > img.naturalHeight) {
              card.classList.add("wide-card");
            }
          };
          img.addEventListener("load", probe);
          if (img.complete && img.naturalWidth > 0) probe();
          return img;
        })()
      : h("div", { class: "cover skeleton" }),
    badge ? h("span", { class: "badge" }, badge) : null,
    h("div", { class: "meta" },
      h("div", { class: "title", title: title || gridTitle }, gridTitle || `#${id}`),
      h("div", { class: "sub" }, pageCount ? `${pageCount} pages` : "—"),
    ),
  );
  return card;
});
