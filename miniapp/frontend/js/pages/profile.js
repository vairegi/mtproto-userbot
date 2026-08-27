/*
  pages/profile.js — User profile page   (v12.36)

  v12.36 — task brief: the visible Saved Files grid AND the Badges (x/y)
  block are removed from the Profile tab. Saved-files browsing already
  lives in the Saved tab via /api/bookmarks; Activity + Leaderboard are
  the only Profile surfaces now.

  Top Queuers Today (top 11) was promoted here from the Admin tab; it
  reads from /api/stats/leaderboard which is a public per-user endpoint.
  Badge logic is gone from the file entirely.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { store } from "core/state.js";
import { openGalleryDetail } from "plugins/detail-sheet.js?v=12.57";  // v11.9
// v12.3: prefetchGallery import removed — no more background warming storm.

export async function render(root, { me }) {
  const $hero = h("div", { class: "profile-hero" });
  const $stats = h("div", { style: { marginTop: "16px" } });     // v11.7 (kept)
  const $leader = h("div", { style: { marginTop: "16px" } }); // v12.36
  root.append($hero, $stats, $leader);

  paintHero(me);
  paintStats(null);       // skeleton first
  paintLeaderboard(null); // v12.36: skeleton first

  try {
    const [fresh, stats, leaderboard] = await Promise.all([
      api.get("/api/profile/me"),
      // /api/stats/me still serves saves/ratings/shares/streak — we still
      // need it for the Your Activity tiles (v11.7 contract preserved).
      api.get("/api/stats/me").catch(() => null),
      // v12.36: new endpoint; the previous Top-Queuers widget on the Admin
      // tab was promoted here and the cap raised to 11 users (was 5).
      api.get("/api/stats/leaderboard?limit=11").catch(() => null),
    ]);
    paintHero(fresh);
    store.set("me", fresh);
    if (stats) paintStats(stats);
    if (leaderboard) paintLeaderboard(leaderboard.items || []);
  } catch (e) {
    console.warn("profile refresh failed:", e);
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

  /* v12.36 ---------------------------------------------------------------
     Leaderboard ("Top Queuers Today") promoted here from admin.js.
     Limit raised 5 → 11 to match the operator's brief. Renders the top
     11 users ranked by today's queue count.
     ----------------------------------------------------------------------- */
  function paintLeaderboard(rows) {
    $leader.innerHTML = "";
    $leader.appendChild(h("h3", {
      style: { margin: "0 0 10px", fontSize: "15px",
               color: "var(--du-ink-mid)", fontWeight: "600" }
    }, "🏆 Top Queuers Today"));
    if (!rows || !rows.length) {
      $leader.appendChild(h("div", { class: "empty" },
        h("div", { class: "icon" }, "🪶"),
        h("div", { class: "title" }, "No queues today yet"),
        h("div", {}, "Be the first to break the silence."),
      ));
      return;
    }
    const wrap = h("div", {
      style: {
        background: "var(--du-bg-2)",
        border: "1px solid var(--du-border)",
        borderRadius: "12px",
        padding: "12px",
      },
    });
    rows.forEach(function (r, idx) {
      const who = r.username
        ? ("@" + r.username)
        : (r.first_name || ("#" + r.user_id));
      const rank = String(idx + 1).padStart(2, "0");
      const row = h("div", {
        class: "kv-row",
        style: {
          padding: "6px 0",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          borderBottom: idx === rows.length - 1
            ? "none"
            : "1px solid var(--du-border)",
        },
      },
        h("span", {
          class: "k",
          style: { color: "var(--du-ink-mid)",
                   fontVariantNumeric: "tabular-nums",
                   width: "28px",
                   textAlign: "right",
                   marginRight: "10px" },
        }, rank + "."),
        h("span", {
          class: "k",
          style: { color: "var(--du-ink-hi)", flex: "1" },
          title: "User ID: " + r.user_id,
        }, who + " · " + r.user_id),
        h("span", {
          class: "v",
          style: { color: "var(--du-ink-hi)",
                   fontVariantNumeric: "tabular-nums" },
        }, r.count + " queues"),
      );
      wrap.appendChild(row);
    });
    $leader.appendChild(wrap);
  }
}
