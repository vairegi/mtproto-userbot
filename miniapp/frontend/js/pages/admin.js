/*
  pages/admin.js — Admin-only control panel

  Only rendered if backend confirms is_admin=true (enforced in registry.js
  by the adminOnly flag, and again server-side on every /api/admin/* call).

  Features:
    - Public / Private toggle (whole app visibility for non-admins)
    - Global rate-limit config (default per-user daily quota)
    - Per-user rate-limit table (view, reset, override)
    - Diagnostics ping (equivalent to /diag in the bot)
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { haptic } from "core/telegram.js";

const toast = (t, k) => make("toast", { text: t, kind: k });

export async function render(root, { me }) {
  root.appendChild(sectionStats());
  root.appendChild(sectionVisibility());
  root.appendChild(sectionRateLimits());
  root.appendChild(sectionUsers());
  root.appendChild(sectionDiag());
}

// -------- 0. KPI stats (uses /api/admin/stats) --------
function sectionStats() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "📊 Overview"));
  const grid = h("div", {
    style: { display: "grid", gridTemplateColumns: "repeat(2, 1fr)",
             gap: "8px", marginBottom: "12px" },
  });
  const top = h("div", {});
  wrap.append(grid, top);

  (async () => {
    try {
      const s = await api.get("/api/admin/stats");
      grid.innerHTML = "";
      const cells = [
        ["Total users",   s.totals.users],
        ["Active today",  s.totals.active_today],
        ["Bookmarks",     s.totals.bookmarks],
        ["Banned",        s.totals.banned],
      ];
      for (const [k, v] of cells) {
        grid.appendChild(h("div", {
          style: { background: "var(--du-bg-2)",
                   border: "1px solid var(--du-border)",
                   borderRadius: "12px", padding: "12px",
                   display: "flex", flexDirection: "column", gap: "2px" },
        },
          h("div", { style: { color: "var(--du-ink-lo)", fontSize: "11px" } }, k),
          h("div", { style: { fontSize: "20px", fontWeight: "700",
                              color: "var(--du-ink-hi)" } }, String(v ?? 0)),
        ));
      }
      if (s.top_queuers_today && s.top_queuers_today.length) {
        top.appendChild(h("div", { style: { color: "var(--du-ink-mid)",
            fontSize: "12px", fontWeight: "600", margin: "8px 0 4px" } },
          "🔥 Top queuers today"));
        for (const r of s.top_queuers_today) {
          top.appendChild(h("div", { class: "kv-row" },
            h("span", { class: "k" }, "#" + r.user_id),
            h("span", { class: "v" }, `${r.count} queues`),
          ));
        }
      }
    } catch (e) {
      grid.innerHTML = "";
      grid.appendChild(h("div", {}, "Failed: " + e.message));
    }
  })();

  return wrap;
}

// -------- 1. Visibility (public/private) --------
function sectionVisibility() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "🌐 App Visibility"));

  const row = h("div", { class: "kv-row" });
  const status = h("span", { class: "v" }, "…");
  const toggle = h("button", {
    class: "toggle",
    "aria-checked": "false",
    "aria-label": "Toggle public mode",
  });
  row.append(
    h("span", { class: "k" }, "Public mode (all users can use the app)"),
    toggle,
  );
  wrap.append(row, h("div", { class: "kv-row" },
    h("span", { class: "k" }, "Current"),
    status,
  ));

  (async () => {
    try {
      const s = await api.get("/api/admin/visibility");
      const on = !!s.public_mode;
      toggle.setAttribute("aria-checked", on ? "true" : "false");
      status.textContent = on ? "PUBLIC" : "PRIVATE";
    } catch (e) { status.textContent = "error: " + e.message; }
  })();

  toggle.addEventListener("click", async () => {
    const cur = toggle.getAttribute("aria-checked") === "true";
    const next = !cur;
    toggle.setAttribute("aria-checked", next ? "true" : "false");
    haptic("medium");
    try {
      await api.post("/api/admin/visibility", { public_mode: next });
      status.textContent = next ? "PUBLIC" : "PRIVATE";
      toast(next ? "App is now PUBLIC" : "App is now PRIVATE (admin only)", "success");
    } catch (e) {
      toggle.setAttribute("aria-checked", cur ? "true" : "false");
      toast("Failed: " + e.message, "error");
    }
  });

  return wrap;
}

// -------- 2. Rate limits (defaults) --------
function sectionRateLimits() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "⏱️ Rate Limits (defaults)"));

  const dailyInput = h("input", {
    type: "number", min: "0", inputmode: "numeric",
    style: { width: "80px", background: "var(--du-bg-2)",
             padding: "4px 8px", borderRadius: "8px",
             border: "1px solid var(--du-border)", color: "var(--du-ink-hi)",
             textAlign: "right" },
  });
  const cooldownInput = h("input", {
    type: "number", min: "0", inputmode: "numeric",
    style: { width: "80px", background: "var(--du-bg-2)",
             padding: "4px 8px", borderRadius: "8px",
             border: "1px solid var(--du-border)", color: "var(--du-ink-hi)",
             textAlign: "right" },
  });

  wrap.append(
    h("div", { class: "kv-row" },
      h("span", { class: "k" }, "Default queues per user / day"),
      dailyInput,
    ),
    h("div", { class: "kv-row" },
      h("span", { class: "k" }, "Cooldown between queues (seconds)"),
      cooldownInput,
    ),
    h("button", {
      class: "btn primary block",
      style: { marginTop: "12px" },
      onclick: async () => {
        haptic("medium");
        try {
          await api.post("/api/admin/ratelimit/defaults", {
            daily: parseInt(dailyInput.value, 10) || 0,
            cooldown_s: parseInt(cooldownInput.value, 10) || 0,
          });
          toast("Defaults saved", "success");
        } catch (e) { toast("Failed: " + e.message, "error"); }
      },
    }, "Save defaults"),
  );

  (async () => {
    try {
      const s = await api.get("/api/admin/ratelimit/defaults");
      dailyInput.value = s.daily ?? 20;
      cooldownInput.value = s.cooldown_s ?? 0;
    } catch (_) {
      dailyInput.value = 20;
      cooldownInput.value = 0;
    }
  })();

  return wrap;
}

// -------- 3. Per-user rate-limit table --------
function sectionUsers() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "👥 Users"));

  const list = h("div", {});
  const empty = h("div", { class: "empty" },
    h("div", { class: "icon" }, "👤"),
    h("div", { class: "title" }, "No users yet"),
  );
  wrap.append(list);

  async function refresh() {
    list.innerHTML = "";
    try {
      const res = await api.get("/api/admin/users");
      const users = res.items || [];
      if (!users.length) { list.appendChild(empty); return; }
      for (const u of users) list.appendChild(userRow(u, refresh));
    } catch (e) {
      list.appendChild(h("div", {}, "Failed: " + e.message));
    }
  }
  refresh();
  return wrap;
}

function userRow(u, refresh) {
  const row = h("div", {
    style: { padding: "10px 0", borderBottom: "1px solid var(--du-divider)",
             display: "flex", flexDirection: "column", gap: "6px" },
  },
    h("div", { style: { display: "flex", justifyContent: "space-between",
                        alignItems: "center" } },
      h("div", {},
        h("div", { style: { fontWeight: "600" } },
          (u.first_name || u.username || "user") + " · " + u.user_id),
        h("div", { style: { color: "var(--du-ink-lo)", fontSize: "12px" } },
          `Used ${u.used_today || 0} / ${u.limit || "∞"} today`),
      ),
      h("div", { style: { display: "flex", gap: "6px" } },
        h("button", {
          class: "btn ghost",
          onclick: async () => {
            haptic("light");
            try { await api.post(`/api/admin/users/${u.user_id}/reset`);
                  refresh(); toast("Reset", "success"); }
            catch (e) { toast(e.message, "error"); }
          },
        }, "Reset"),
        h("button", {
          class: "btn secondary",
          onclick: async () => {
            const val = prompt("New daily limit for this user (0 = unlimited)", String(u.limit || 20));
            if (val === null) return;
            haptic("medium");
            try { await api.post(`/api/admin/users/${u.user_id}/limit`,
                                 { daily: parseInt(val, 10) || 0 });
                  refresh(); toast("Updated", "success"); }
            catch (e) { toast(e.message, "error"); }
          },
        }, "Set"),
        u.banned
          ? h("button", { class: "btn secondary",
              onclick: async () => {
                haptic("light");
                try { await api.post(`/api/admin/users/${u.user_id}/unban`);
                      refresh(); }
                catch (e) { toast(e.message, "error"); }
              } }, "Unban")
          : h("button", { class: "btn danger",
              onclick: async () => {
                if (!confirm("Ban this user from the app?")) return;
                haptic("warning");
                try { await api.post(`/api/admin/users/${u.user_id}/ban`);
                      refresh(); }
                catch (e) { toast(e.message, "error"); }
              } }, "Ban"),
      ),
    ),
  );
  return row;
}

// -------- 4. Diagnostics --------
function sectionDiag() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "🩺 Diagnostics"));
  const out = h("pre", {
    style: { background: "var(--du-bg-2)", padding: "12px",
             borderRadius: "8px", fontSize: "12px", overflowX: "auto",
             color: "var(--du-ink-mid)", margin: "0" },
  }, "Tap Run to probe the scraper + queue…");
  const btn = h("button", { class: "btn primary block",
    style: { marginTop: "12px" },
    onclick: async () => {
      haptic("medium");
      out.textContent = "Running…";
      try {
        const r = await api.get("/api/admin/diag");
        out.textContent = JSON.stringify(r, null, 2);
      } catch (e) { out.textContent = "ERROR\n" + (e.message || e); }
    },
  }, "Run diagnostics");
  wrap.append(out, btn);
  return wrap;
}
