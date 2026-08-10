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

  // v12.12 (#3): TDZ fix. v12.10's wide-card IIFE ran INSIDE the h(...)
  // children arguments, and its closure captured `card` — a const whose
  // initialization only completes AFTER h() returns. If the cover probe
  // fired synchronously (cached image), it touched the binding while it
  // was still in the temporal dead zone, throwing
  // "Cannot access 'card' before initialization" and killing the whole
  // page render. Fix: build the img first, then the article, and attach
  // the probe AFTER `card` is assigned.
  const imgEl = cover
    ? h("img", { class: "cover", src: cover, loading: "lazy", alt: title || "cover" })
    : h("div", { class: "cover skeleton" });

  const card = h("article", {
    class: "gallery-card",
    dataset: { galleryId: id },
    onclick: () => { haptic("light"); onOpen && onOpen(props); },
  },
    imgEl,
    badge ? h("span", { class: "badge" }, badge) : null,
    h("div", { class: "meta" },
      h("div", { class: "title", title: title || gridTitle }, gridTitle || `#${id}`),
      h("div", { class: "sub" }, pageCount ? `${pageCount} pages` : "—"),
    ),
  );

  // v12.10 (#9, option A): landscape covers get .wide-card so the card
  // spans the full grid row (see components.css). The probe runs on load
  // AND immediately for cached images (complete flag), so the class lands
  // before first paint whenever possible. v12.12: safe now — `card` is
  // fully initialized by the time any of these run.
  if (cover && imgEl instanceof HTMLImageElement) {
    const probe = () => {
      if (imgEl.naturalWidth > 0 && imgEl.naturalWidth > imgEl.naturalHeight) {
        card.classList.add("wide-card");
      }
    };
    imgEl.addEventListener("load", probe);
    if (imgEl.complete && imgEl.naturalWidth > 0) probe();
  }

  return card;
});
