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
  root.appendChild(sectionAutoDelete());
  root.appendChild(sectionShareGuard());
  root.appendChild(sectionForceJoin());
  root.appendChild(sectionUsers());
  root.appendChild(sectionDiag());
}

// Small helpers shared by the new feature sections ------------------------
function makeToggle(initial, onFlip) {
  const t = h("button", {
    class: "toggle",
    "aria-checked": initial ? "true" : "false",
    "aria-label": "Toggle",
    onclick: async () => {
      const cur = t.getAttribute("aria-checked") === "true";
      const next = !cur;
      t.setAttribute("aria-checked", next ? "true" : "false");
      haptic("light");
      try { await onFlip(next); }
      catch (e) {
        // Roll back on failure.
        t.setAttribute("aria-checked", cur ? "true" : "false");
        toast(e.message, "error");
      }
    },
  });
  return t;
}

// -------- 3. Auto-delete (feature 1) --------
function sectionAutoDelete() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "⏱️ Auto-delete DM'd files"));

  const row = h("div", { class: "kv-row" });
  const toggle = makeToggle(false, async (on) => {
    const hours = parseInt(hoursInput.value || "24", 10) || 24;
    await api.post("/api/admin/autodelete", { enabled: on, hours });
    toast(on ? "Auto-delete ON" : "Auto-delete OFF", on ? "success" : "");
  });
  row.append(
    h("span", { class: "k" }, "Auto-delete files sent to users"),
    toggle,
  );

  const hoursRow = h("div", { class: "kv-row" });
  const hoursInput = h("input", {
    type: "number", min: "1", max: "720", step: "1",
    style: { width: "80px" }, value: "24",
  });
  const hoursSave = h("button", { class: "btn",
    onclick: async () => {
      const enabled = toggle.getAttribute("aria-checked") === "true";
      const hours = parseInt(hoursInput.value || "24", 10) || 24;
      if (hours < 1) { toast("Hours must be ≥ 1", "error"); return; }
      haptic("medium");
      try {
        await api.post("/api/admin/autodelete", { enabled, hours });
        toast(`Auto-delete every ${hours}h saved`, "success");
      } catch (e) { toast(e.message, "error"); }
    },
  }, "Save");
  hoursRow.append(
    h("span", { class: "k" }, "Delete after (hours)"),
    hoursInput, hoursSave,
  );

  wrap.append(row, hoursRow);

  (async () => {
    try {
      const s = await api.get("/api/admin/autodelete");
      toggle.setAttribute("aria-checked", s.enabled ? "true" : "false");
      hoursInput.value = String(s.hours || 24);
    } catch (e) { toast(e.message, "error"); }
  })();

  return wrap;
}

// -------- 4. Disable sharing (feature 2) --------
function sectionShareGuard() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "🔒 Disable sharing"));

  const row = h("div", { class: "kv-row" });
  const toggle = makeToggle(false, async (on) => {
    await api.post("/api/admin/shareguard", { enabled: on });
    toast(on ? "Users can no longer forward/save files" : "Sharing re-enabled",
          on ? "success" : "");
  });
  row.append(
    h("span", { class: "k" }, "Users cannot share post or file"),
    toggle,
  );

  const hint = h("div", {
    style: { color: "var(--du-ink-lo)", fontSize: "11px", marginTop: "4px" },
  }, "When ON, Telegram blocks forwarding / saving on everything the bot sends.");

  wrap.append(row, hint);

  (async () => {
    try {
      const s = await api.get("/api/admin/shareguard");
      toggle.setAttribute("aria-checked", s.enabled ? "true" : "false");
    } catch (e) { toast(e.message, "error"); }
  })();

  return wrap;
}

// -------- 5. Force-join channels (feature 3) --------
function sectionForceJoin() {
  const wrap = h("div", { class: "admin-section" });
  wrap.appendChild(h("h3", {}, "👥 Force-join channel"));

  const list = h("div", {});
  const hint = h("div", {
    style: { color: "var(--du-ink-lo)", fontSize: "11px", margin: "4px 0 8px" },
  }, "Users must join these channels before the bot DMs them any file. "
   + "Add a public @handle, a private invite link (t.me/+…), "
   + "or a numeric -100… channel ID. Bot must be admin in each channel. "
   + "For private channels added by numeric ID, also paste an invite link "
   + "(t.me/+…) below — otherwise the Join button won't work for non-members.");

  const input = h("input", {
    type: "text",
    placeholder: "@channel  OR  https://t.me/+abcXYZ  OR  -1001234567890",
    style: { flex: "1", minWidth: "0" },
  });
  // Improvement #6: dedicated field for a joinable invite URL. When the
  // channel is a numeric -100… ID (a private channel), the button we send
  // users would otherwise fall back to a t.me/c/<internal> link, which is
  // NOT joinable for non-members. Admins can paste a real t.me/+… invite
  // link here so the button actually works.
  const inviteInput = h("input", {
    type: "text",
    placeholder: "Optional invite link (https://t.me/+…) — required for private -100… channels",
    style: { flex: "1", minWidth: "0" },
  });
  const addBtn = h("button", { class: "btn primary",
    onclick: async () => {
      const v  = (input.value || "").trim();
      const iv = (inviteInput.value || "").trim();
      if (!v) { toast("Enter a channel handle first", "error"); return; }
      // Send both tokens on one line; the backend's _split_channel_and_invite()
      // separates them and stores the invite hash for the Join button.
      const payload = iv ? (v + " " + iv) : v;
      haptic("medium");
      try {
        const r = await api.post("/api/admin/forcejoin/add",
                                 { channel: payload });
        input.value = "";
        inviteInput.value = "";
        toast(r.already ? "Already in the list" : "Channel added", "success");
        renderList(r.channels || []);
      } catch (e) { toast(e.message, "error"); }
    },
  }, "Add");

  const addRow = h("div", {
    style: { display: "flex", gap: "8px", alignItems: "center" },
  }, input, addBtn);
  const inviteRow = h("div", {
    style: { display: "flex", gap: "8px", alignItems: "center",
             marginTop: "6px" },
  }, inviteInput);

  function renderList(channels) {
    list.innerHTML = "";
    if (!channels.length) {
      list.appendChild(h("div", {
        style: { color: "var(--du-ink-lo)", fontSize: "12px" },
      }, "No force-join channels — feature is OFF."));
      return;
    }
    for (const c of channels) {
      const label = c.title || (c.username ? "@" + c.username
                                             : (c.invite_hash
                                                ? "Private channel (+" + c.invite_hash.slice(0,6) + "…)"
                                                : ("#" + (c.chat_id || ""))));
      const removeBtn = h("button", { class: "btn danger",
        onclick: async () => {
          // Improvement (Bug 2 fix): invite-hash-only rows have empty
          // username AND null chat_id, so the previous key derivation
          // returned "" and the Remove button silently no-op'd. Fall
          // back to a t.me/+<hash> string here — the backend's
          // _split_channel_and_invite() + _normalise_handle() already
          // decode that shape correctly.
          let key = c.username
                 || (c.chat_id ? String(c.chat_id) : "")
                 || (c.invite_hash ? "https://t.me/+" + c.invite_hash : "");
          if (!key) { toast("Cannot identify this row — please reload", "error"); return; }
          haptic("warning");
          try {
            const r = await api.post("/api/admin/forcejoin/remove",
                                     { channel: key });
            toast(r.removed ? "Channel removed" : "Nothing to remove",
                  r.removed ? "success" : "");
            renderList(r.channels || []);
          } catch (e) { toast(e.message, "error"); }
        },
      }, "Remove");
      list.appendChild(h("div", { class: "kv-row" },
        h("span", { class: "k" }, label),
        removeBtn,
      ));
    }
  }

  wrap.append(hint, list, addRow, inviteRow);

  (async () => {
    try {
      const s = await api.get("/api/admin/forcejoin");
      renderList(s.channels || []);
    } catch (e) { toast(e.message, "error"); }
  })();

  return wrap;
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

// -------- 3. Per-user rate-limit table (collapsible, collapsed by default) --------
function sectionUsers() {
  const wrap = h("div", { class: "admin-section" });

  // Improvement (Bug 3): make the whole Users section collapsible. The
  // header row is now a button-styled div with a rotating caret; the
  // list body is hidden by default and only fetched on first expand,
  // so the (potentially large) /api/admin/users request doesn't fire
  // just because the admin scrolled past.
  const caret = h("span", {
    style: {
      display: "inline-block",
      transition: "transform 0.18s ease",
      transform: "rotate(-90deg)",   // ▼ rotated → points right when collapsed
      marginLeft: "6px",
      fontSize: "12px",
      color: "var(--du-ink-mid)",
    },
  }, "▼");

  const header = h("h3", {
    style: { display: "flex", alignItems: "center",
             justifyContent: "space-between", cursor: "pointer",
             userSelect: "none", margin: "0" },
    role: "button",
    tabindex: "0",
    "aria-expanded": "false",
  },
    h("span", {}, "👥 Users"),
    caret,
  );

  const body = h("div", {
    style: { display: "none", marginTop: "8px" },
  });
  const list = h("div", {});
  const empty = h("div", { class: "empty" },
    h("div", { class: "icon" }, "👤"),
    h("div", { class: "title" }, "No users yet"),
  );
  body.appendChild(list);

  let expanded = false;
  let everFetched = false;

  async function refresh() {
    list.innerHTML = "";
    list.appendChild(h("div", {
      style: { color: "var(--du-ink-lo)", fontSize: "12px", padding: "6px 0" },
    }, "Loading users…"));
    try {
      const res = await api.get("/api/admin/users");
      const users = res.items || [];
      list.innerHTML = "";
      if (!users.length) { list.appendChild(empty); return; }
      for (const u of users) list.appendChild(userRow(u, refresh));
    } catch (e) {
      list.innerHTML = "";
      list.appendChild(h("div", {}, "Failed: " + e.message));
    }
  }

  function toggle() {
    expanded = !expanded;
    body.style.display = expanded ? "block" : "none";
    caret.style.transform = expanded ? "rotate(0deg)" : "rotate(-90deg)";
    header.setAttribute("aria-expanded", expanded ? "true" : "false");
    haptic("light");
    if (expanded && !everFetched) {
      everFetched = true;
      refresh();
    }
  }

  header.addEventListener("click", toggle);
  header.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
  });

  wrap.append(header, body);
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
