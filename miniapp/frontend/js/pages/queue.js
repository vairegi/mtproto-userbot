/*
  pages/queue.js — Live view of pending / processing / done jobs

  Refreshes every 5s while the page is mounted (teardown returns a cleanup fn).
  Backend: /api/queue/status returns counts + a recent-jobs list.

  V2 additions:
    - Completed rows render an "Open Post" button that opens the DB channel
      deep-link (queue_bridge._row now embeds `open_link`).
    - Failed rows show the user-friendly reason (relay_v2 writes friendly
      strings; technical text stays admin-only).
    - PROCESSING rows show a spinner-style badge so users know work is
      in flight, not stuck.
*/

import { api } from "core/api.js";
import { h, make } from "core/components.js";
import { openLink } from "core/telegram.js";
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
      $list.appendChild(renderJob(j));
    }
  }

  function renderJob(j) {
    const status = String(j.status || "").toLowerCase();
    const isDone       = status === "done" || status === "completed";
    const isProcessing = status === "processing";
    const isFailed     = status === "failed";
    const isPartial    = status === "partial";

    // Header row: id + status pill.
    const header = h("div", { style: {
      display: "flex", justifyContent: "space-between",
      alignItems: "center", marginBottom: "6px",
    }},
      h("span", { style: { fontWeight: "600" } },
        "#" + (j.id || j._id || "")),
      h("span", { class: "hdr-badge " + statusClass(j.status) },
        isProcessing ? "⏳ processing" : (j.status || "?")),
    );

    // Title (or fallback to url).
    const titleLine = j.title
      ? h("div", { style: {
            color: "var(--du-ink-mid)", fontSize: "13px", fontWeight: "500",
        }}, j.title)
      : null;

    // The gallery URL, truncated.
    const urlLine = j.url
      ? h("div", { class: "u-truncate",
          style: { color: "var(--du-ink-lo)", fontSize: "12px", marginTop: "2px" }},
          j.url)
      : null;

    // Failure reason line (only present on failed rows; relay_v2 writes
    // friendly text on `error_reason`, but keep it small).
    const reasonLine = (isFailed && j.error_reason)
      ? h("div", { style: {
            color: "var(--du-danger, #d33)", fontSize: "12px", marginTop: "4px",
        }}, j.error_reason)
      : null;

    // Action row: Open Post for completed / partial rows.
    let actions = null;
    if ((isDone || isPartial) && j.open_link) {
      actions = h("div", { style: { marginTop: "8px" } },
        h("button", {
          class: "btn primary",
          onClick: () => openLink(j.open_link),
        }, "🔗 Open Post"),
      );
    } else if ((isDone || isPartial) && !j.open_link && j.gallery_id) {
      // No cover_link stored on the job row — offer a fallback: fetch it on
      // demand via /api/gallery/{id}/status (one RTT, then open).
      actions = h("div", { style: { marginTop: "8px" } },
        h("button", {
          class: "btn secondary",
          onClick: async (ev) => {
            const btn = ev.currentTarget;
            btn.disabled = true;
            try {
              const s = await api.get(`/api/gallery/${j.gallery_id}/status`);
              if (s && s.open_link) openLink(s.open_link);
              else btn.textContent = "No post link";
            } catch (e) {
              btn.textContent = "Failed to open";
            }
          },
        }, "🔗 Locate Post"),
      );
    }

    return h("div", { class: "admin-section" },
      header, titleLine, urlLine, reasonLine, actions,
    );
  }

  await tick();
  const t = setInterval(tick, 5000);
  return () => clearInterval(t);
}

function statusClass(s) {
  const v = String(s || "").toLowerCase();
  if (v === "processing") return "warn";
  if (v === "failed")     return "danger";
  if (v === "partial")    return "warn";
  if (v === "done" || v === "completed") return "success";
  return "";
}
