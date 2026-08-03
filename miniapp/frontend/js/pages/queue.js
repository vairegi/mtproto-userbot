/*
  pages/queue.js — Live view of pending / processing / done jobs

  Refreshes every 5s while the page is mounted (teardown returns a cleanup fn).
  Backend: /api/queue/status returns counts + a recent-jobs list.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { store } from "core/state.js";

export async function render(root, { me }) {
  const $summary = h("div", { class: "admin-section" });
  const $list = h("div", {});
  root.append($summary, $list);
  $list.appendChild(make("skeleton", { height: 60 }));

  async function tick() {
    try {
      const s = await api.get("/api/queue/status");
      store.set("queue_status", s);
      renderSummary(s);
      renderList(s.recent || []);
    } catch (e) {
      $summary.innerHTML = "";
      $summary.appendChild(h("div", { class: "kv-row" },
        h("span", { class: "k" }, "Error"),
        h("span", { class: "v" }, e.message || String(e)),
      ));
    }
  }

  function renderSummary(s) {
    $summary.innerHTML = "";
    const rows = [
      ["Pending",    s.pending || 0],
      ["Processing", s.processing || 0],
      ["Completed",  s.completed || 0],
      ["Failed",     s.failed || 0],
    ];
    for (const [k, v] of rows) {
      $summary.appendChild(h("div", { class: "kv-row" },
        h("span", { class: "k" }, k),
        h("span", { class: "v" }, String(v)),
      ));
    }
  }

  function renderList(items) {
    $list.innerHTML = "";
    if (!items.length) {
      $list.appendChild(h("div", { class: "empty" },
        h("div", { class: "icon" }, "📭"),
        h("div", { class: "title" }, "No recent jobs"),
      ));
      return;
    }
    for (const j of items) {
      $list.appendChild(h("div", { class: "admin-section" },
        h("div", { style: { display: "flex", justifyContent: "space-between",
                            alignItems: "center", marginBottom: "6px" } },
          h("span", { style: { fontWeight: "600" } }, "#" + (j.id || j._id || "")),
          h("span", { class: "hdr-badge " + statusClass(j.status) }, j.status || "?"),
        ),
        h("div", { class: "u-truncate", style: { color: "var(--du-ink-mid)",
                                                 fontSize: "13px" } }, j.url || ""),
        j.title ? h("div", { style: { color: "var(--du-ink-lo)",
                                      fontSize: "12px", marginTop: "4px" } }, j.title) : null,
      ));
    }
  }

  await tick();
  const t = setInterval(tick, 5000);
  return () => clearInterval(t);
}

function statusClass(s) {
  if (s === "processing") return "warn";
  if (s === "failed") return "danger";
  return "";
}
