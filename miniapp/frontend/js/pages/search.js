// v12.34b — propagate is_cached into the card factory props bag.
// Patch the renderCard() local helper ONLY; do not touch API wiring.
_private const RENDER_CARD_OLD = `  function renderCard(g) {
    return make("card", {
      id: g.id,
      title: g.title,
      cover: g.cover,
      pages: g.pages,
      badge: g.badge ?? null,
      is_cached: typeof g.is_cached === "boolean" ? g.is_cached : false,
      onOpen: () => openDetail(g),
    });
  }`;
_private const RENDER_CARD_NEW = `  function renderCard(g) {
    return make("card", {
      id: g.id,
      title: g.title,
      cover: g.cover,
      pages: g.pages,
      badge: g.badge ?? null,
      is_cached: typeof g.is_cached === "boolean" ? g.is_cached : false,
      onOpen: () => openDetail(g),
    });
  }`;
