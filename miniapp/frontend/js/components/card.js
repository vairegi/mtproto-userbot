/*
  card.js — Gallery card component

  Props:
    id          nhentai gallery id
    title       cleaned title
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

  const card = h("article", {
    class: "gallery-card",
    dataset: { galleryId: id },
    onclick: () => { haptic("light"); onOpen && onOpen(props); },
  },
    cover
      ? h("img", { class: "cover", src: cover, loading: "lazy", alt: title || "cover" })
      : h("div", { class: "cover skeleton" }),
    badge ? h("span", { class: "badge" }, badge) : null,
    h("div", { class: "meta" },
      h("div", { class: "title", title }, title || `#${id}`),
      h("div", { class: "sub" }, pageCount ? `${pageCount} pages` : "—"),
    ),
  );
  return card;
});
