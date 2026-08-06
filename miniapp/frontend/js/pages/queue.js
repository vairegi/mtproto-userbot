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

  // v10: per-job progress poll registry. Each PROCESSING row spins up
  // its own 2.5s interval that hits /api/queue/progress/<gallery_id>
  // and updates a small card underneath the row. Cleaned up on page
  // teardown via the same returned cleanup fn.
  const progressTimers = new Map();

  function stopProgress(galleryId) {
    const t = progressTimers.get(String(galleryId));
    if (t) { clearInterval(t); progressTimers.delete(String(galleryId)); }
  }

  function startProgress(galleryId, mount) {
    const key = String(galleryId);
    if (!key || key === "undefined" || key === "null") return;
    if (progressTimers.has(key)) return;  // already polling
    const tick = async () => {
      try {
        const s = await api.get("/api/queue/progress/" + encodeURIComponent(key));
        renderProgressCard(mount, s);
        if (!s.is_active) {
          stopProgress(key);   // finished or failed — stop polling
        }
      } catch (e) {
        // One-off poll failure — keep trying, the row may still be active.
      }
    };
    tick();
    progressTimers.set(key, setInterval(tick, 2500));
  }

  function renderProgressCard(mount, s) {
    if (!mount) return;
    const pct = (typeof s.pct === "number") ? Math.max(0, Math.min(100, s.pct)) : null;
    const bar = pct === null ? null : h("div", {
      style: {
        marginTop: "8px", height: "6px", background: "var(--du-bg-2)",
        borderRadius: "999px", overflow: "hidden",
      },
    },
      h("div", {
        style: {
          height: "100%", width: pct + "%",
          background: "linear-gradient(90deg, var(--du-accent), var(--du-accent-2))",
          transition: "width 0.4s ease",
        },
      }),
    );
    mount.innerHTML = "";
    mount.appendChild(h("div", {
      style: {
        marginTop: "10px", padding: "10px 12px",
        background: "var(--du-bg-2)", borderRadius: "10px",
        border: "1px solid var(--du-border)",
      },
    },
      h("div", {
        style: { fontSize: "13px", fontWeight: "600",
                 color: "var(--du-ink-hi)",
                 display: "flex", alignItems: "center", gap: "6px" },
      },
        s.is_done ? "✅" : (s.is_failed ? "❌" : "⏳"),
        h("span", {}, s.human || "Working…"),
      ),
      s.detail ? h("div", {
        style: { fontSize: "11px", color: "var(--du-ink-lo)", marginTop: "4px" },
      }, s.detail) : null,
      bar,
      pct !== null ? h("div", {
        style: { fontSize: "10px", color: "var(--du-ink-lo)", marginTop: "4px" },
      }, pct + "% complete") : null,
      (s.is_failed && s.failed_reason) ? h("div", {
        style: { fontSize: "11px", color: "var(--du-danger, #d33)", marginTop: "6px" },
      }, "Error: " + s.failed_reason) : null,
    ));
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
          class: "btn primary btn-glow btn-ripple",
          onClick: () => openLink(j.open_link),
        }, "🔗 Open Post"),
      );
    } else if ((isDone || isPartial) && !j.open_link && j.gallery_id) {
      // No cover_link stored on the job row — offer a fallback: fetch it on
      // demand via /api/gallery/{id}/status (one RTT, then open).
      actions = h("div", { style: { marginTop: "8px" } },
        h("button", {
          class: "btn secondary btn-lift",
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

    // v10: live progress card for PROCESSING rows. The card updates in
    // place every 2.5s via /api/queue/progress/<gallery_id> — when the
    // worker finishes, the next 5s tick() refresh will replace the row
    // with a COMPLETED one and stopProgress will fire automatically.
    let progressMount = null;
    if (isProcessing && j.gallery_id) {
      progressMount = h("div", {});
      startProgress(j.gallery_id, progressMount);
    }

    return h("div", { class: "admin-section" },
      header, titleLine, urlLine, reasonLine, actions, progressMount,
    );
  }

  await tick();
  const t = setInterval(tick, 5000);
  return () => {
    clearInterval(t);
    for (const [, timerId] of progressTimers) clearInterval(timerId);
    progressTimers.clear();
  };
}

function statusClass(s) {
  const v = String(s || "").toLowerCase();
  if (v === "processing") return "warn";
  if (v === "failed")     return "danger";
  if (v === "partial")    return "warn";
  if (v === "done" || v === "completed") return "success";
  return "";
}
