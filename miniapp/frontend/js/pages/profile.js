/*
  pages/profile.js — User profile page   (v11.7)

  v11.7 additions:
    * User Stats panel — saves, ratings-given, shares, day-streak
    * Badges grid — unlocked + locked (greyed) with tooltip on hover
    Both come from /api/stats/me. The old "Saved Files" grid stays put.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { store } from "core/state.js";

export async function render(root, { me }) {
  const $hero   = h("div", { class: "profile-hero" });
  const $stats  = h("div", { style: { marginTop: "16px" } });   // v11.7
  const $badges = h("div", { style: { marginTop: "16px" } });   // v11.7
  const $saved  = h("div", { style: { marginTop: "20px" } });
  root.append($hero, $stats, $badges, $saved);

  paintHero(me);
  paintStats(null);      // skeleton first
  paintBadges(null);

  try {
    const [fresh, bookmarks, stats] = await Promise.all([
      api.get("/api/profile/me"),
      api.get("/api/bookmarks"),
      api.get("/api/stats/me").catch(() => null),
    ]);
    paintHero(fresh);
    store.set("me", fresh);
    store.set("bookmarks", bookmarks.items || []);
    paintSaved(bookmarks.items || []);
    if (stats) {
      paintStats(stats);
      paintBadges(stats.badges || []);
    }
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

  /* v11.7 --------------------------------------------------------------- */
  function paintStats(s) {
    $stats.innerHTML = "";
    $stats.appendChild(h("h3", {
      style: { margin: "0 0 10px", fontSize: "15px",
               color: "var(--du-ink-mid)", fontWeight: "600" }
    }, "📊 Your Activity"));
    const values = s || { saves: "—", ratings_given: "—", shares: "—", streak_days: "—" };
    const grid = h("div", {
      style: {
        display: "grid",
        gridTemplateColumns: "repeat(4, minmax(0, 1fr))",
        gap: "8px",
      },
    });
    grid.append(
      tile("📚", values.saves,         "Saved"),
      tile("⭐", values.ratings_given, "Rated"),
      tile("🔗", values.shares,        "Shared"),
      tile("🔥", values.streak_days,   "Streak"),
    );
    $stats.appendChild(grid);
  }

  function tile(icon, value, label) {
    return h("div", {
      style: {
        background: "var(--du-bg-2)",
        border: "1px solid var(--du-border)",
        borderRadius: "10px",
        padding: "10px 6px",
        textAlign: "center",
      },
    },
      h("div", { style: { fontSize: "20px", lineHeight: "1" } }, icon),
      h("div", { style: { fontSize: "18px", fontWeight: "700",
                          color: "var(--du-ink-hi)", marginTop: "4px" } },
        String(value)),
      h("div", { style: { fontSize: "11px", color: "var(--du-ink-lo)",
                          marginTop: "2px" } },
        label),
    );
  }

  function paintBadges(badges) {
    $badges.innerHTML = "";
    if (badges == null) return; // waiting for load
    const unlocked = badges.filter(b => b.unlocked).length;
    $badges.appendChild(h("h3", {
      style: { margin: "0 0 10px", fontSize: "15px",
               color: "var(--du-ink-mid)", fontWeight: "600" }
    }, `🏅 Badges (${unlocked}/${badges.length})`));
    const grid = h("div", {
      style: {
        display: "grid",
        gridTemplateColumns: "repeat(5, minmax(0, 1fr))",
        gap: "8px",
      },
    });
    for (const b of badges) {
      grid.appendChild(h("div", {
        title: `${b.name} — ${b.desc}${b.unlocked ? "" : " (locked)"}`,
        style: {
          background: b.unlocked ? "var(--du-bg-2)" : "var(--du-bg-1)",
          border: "1px solid " + (b.unlocked ? "var(--du-border-strong)" : "var(--du-border)"),
          borderRadius: "10px",
          padding: "10px 4px",
          textAlign: "center",
          opacity: b.unlocked ? "1" : "0.42",
          filter: b.unlocked ? "none" : "grayscale(0.7)",
          transition: "transform 140ms ease, opacity 140ms ease",
        },
      },
        h("div", { style: { fontSize: "22px", lineHeight: "1" } }, b.icon || "🏅"),
        h("div", { style: { fontSize: "10px", color: "var(--du-ink-mid)",
                            marginTop: "3px", fontWeight: "600" } },
          b.name),
      ));
    }
    $badges.appendChild(grid);
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
        class: "btn secondary btn-lift block",
        style: { marginTop: "12px" },
        onclick: () => { location.hash = "#bookmarks"; },
      }, `See all ${items.length} bookmarks →`));
    }
  }
}
