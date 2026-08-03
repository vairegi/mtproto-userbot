/*
  pages/profile.js — User profile page

  Matches the "Gateway / Secure Connection" reference screenshot vibe:
    - starry gradient background
    - large circular avatar with glow
    - callsign + @handle
    - key/value stat rows (User ID, Status, Encryption, Bookmarks, Queued)
    - "Saved Files" section below (grid of bookmarked covers)

  Data comes from /api/profile/me.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { store } from "core/state.js";

export async function render(root, { me }) {
  const $hero = h("div", { class: "profile-hero" });
  const $saved = h("div", { style: { marginTop: "20px" } });
  root.append($hero, $saved);

  // Render immediately with what we already have from boot.
  paintHero(me);

  try {
    const [fresh, bookmarks] = await Promise.all([
      api.get("/api/profile/me"),
      api.get("/api/bookmarks"),
    ]);
    paintHero(fresh);
    store.set("me", fresh);
    store.set("bookmarks", bookmarks.items || []);
    paintSaved(bookmarks.items || []);
  } catch (e) {
    console.warn("profile refresh failed:", e);
    paintSaved(store.get("bookmarks", []));
  }

  function paintHero(u) {
    $hero.innerHTML = "";
    const avatar = u.photo_url
      ? h("img", { class: "profile-avatar", src: u.photo_url, alt: "avatar" })
      : h("div", { class: "profile-avatar u-center",
                   style: { fontSize: "32px", fontWeight: "700" } },
          (u.first_name || u.username || "?").slice(0, 1).toUpperCase());

    $hero.append(
      avatar,
      h("div", { class: "profile-name" },
        (u.first_name || u.username || "Nameless").toString().toUpperCase()),
      u.username ? h("div", { class: "profile-handle" }, "@" + u.username) : null,
      h("div", { class: "profile-stats" },
        stat("User ID",   String(u.user_id || "—")),
        stat("Status",    "Online", { online: true }),
        stat("Access",    u.is_admin ? "Admin" : (u.public_mode ? "Public" : "Restricted")),
        stat("Rate Limit", u.rate_limit
          ? `${u.rate_limit.used || 0}/${u.rate_limit.limit} today`
          : "—"),
        stat("Bookmarks", String(u.stats?.bookmarks ?? 0)),
        stat("Queued",    String(u.stats?.queued ?? 0)),
      ),
      h("div", { style: { marginTop: "16px", paddingTop: "16px",
                          borderTop: "1px solid var(--du-border)",
                          textAlign: "center" } },
        h("div", { style: { fontSize: "13px", color: "var(--du-ink-mid)",
                            fontWeight: "600", marginBottom: "4px" } },
          "🛡️ Secure Session"),
        h("div", { style: { fontSize: "11px", color: "var(--du-ink-lo)" } },
          "Encryption · 256-bit Active"),
      ),
    );
  }

  function stat(k, v, opts = {}) {
    return h("div", { class: "profile-stat" },
      h("div", { class: "k" }, k),
      h("div", { class: "v" + (opts.online ? " online" : "") }, v),
    );
  }

  function paintSaved(items) {
    $saved.innerHTML = "";
    $saved.appendChild(h("h3", {
      style: { margin: "0 0 12px", fontSize: "15px",
               color: "var(--du-ink-mid)", fontWeight: "600" }
    }, `⭐ Saved Files (${items.length})`));
    if (!items.length) {
      $saved.appendChild(h("div", { class: "empty" },
        h("div", { class: "icon" }, "📁"),
        h("div", { class: "title" }, "Nothing saved yet"),
        h("div", {}, "Bookmarked galleries appear here."),
      ));
      return;
    }
    const grid = h("div", { class: "card-grid" });
    for (const g of items.slice(0, 12)) {
      grid.appendChild(make("card", {
        id: g.id, title: g.title, cover: g.cover, pages: g.pages,
        badge: g.pages ? `${g.pages}p` : null,
        onOpen: () => { location.hash = "#bookmarks"; },
      }));
    }
    $saved.appendChild(grid);
    if (items.length > 12) {
      $saved.appendChild(h("button", {
        class: "btn secondary block",
        style: { marginTop: "12px" },
        onclick: () => { location.hash = "#bookmarks"; },
      }, `See all ${items.length} bookmarks →`));
    }
  }
}
