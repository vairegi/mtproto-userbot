/*
  pages/queue.js — v12.76 lightweight rewrite.

  ONE timer polls /api/queue/status every 8s (was: 5s + one 2.5s poller
  PER processing row — that old design opened a fresh Mongo connection
  per poll per row and was the Queue tab's RAM cost on Bot 0).

  The single status response now carries `workers[]` — one card per
  active Bot 2 userbot slot (Bot 2 stamps progress.slot, v12.76) with
  live stage / pages / pct — so the user sees WHICH item each worker is
  processing without any extra requests.

  Completed rows keep a compact "Open" button (deep-link, no RTT).
  Teardown = one clearInterval. Nothing else is registered anywhere.
*/

import { api } from "core/api.js";
import { h } from "core/components.js";
import { openLink } from "core/telegram.js";

const POLL_MS = 8000;

export async function render(root, { me }) {
  const $summary = h("div", { class: "admin-section" });
  const $workers = h("div", {});
  const $list = h("div", {});
  root.append($summary, $workers, $list);

  async function tick() {
    try {
      const s = await api.get("/api/queue/status");
      renderSummary(s);
      renderWorkers(s.workers || []);
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
    for (const [k, v] of [
      ["Pending",    s.pending || 0],
      ["Processing", s.processing || 0],
      ["Completed",  s.completed || 0],
      ["Failed",     s.failed || 0],
    ]) {
      $summary.appendChild(h("div", { class: "kv-row" },
        h("span", { class: "k" }, k),
        h("span", { class: "v" }, String(v)),
      ));
    }
  }

  // ---- workers panel: one compact card per active userbot slot --------
  function renderWorkers(workers) {
    $workers.innerHTML = "";
    if (!workers.length) return;

    const grid = h("div", { style: {
      display: "grid",
      gridTemplateColumns: workers.length > 1 ? "1fr 1fr" : "1fr",
      gap: "8px", marginTop: "10px",
    }});

    workers.forEach((w, i) => {
      const pct = Math.max(0, Math.min(100, Number(w.pct) || 0));
      const label = (w.slot === 0 || w.slot === 1)
        ? "Worker " + (w.slot + 1)
        : "Worker " + (i + 1);
      const title = (w.title && String(w.title).trim()) || ("#" + (w.gid || "?"));
      const pages = (w.page && w.total)
        ? " · " + w.page + "/" + w.total + "p"
        : (w.total ? " · " + w.total + " pages" : "");

      grid.appendChild(h("div", { style: {
        minWidth: 0, padding: "8px 10px",
        background: "var(--du-bg-2)", borderRadius: "10px",
        border: "1px solid var(--du-border)",
      }},
        h("div", { style: {
          fontSize: "10px", fontWeight: "700", letterSpacing: "0.4px",
          color: "var(--du-accent)", textTransform: "uppercase",
          display: "flex", justifyContent: "space-between",
        }},
          h("span", {}, "⚙️ " + label),
          h("span", { style: { color: "var(--du-ink-lo)" } }, pct + "%"),
        ),
        // half title, small size, 2-line clamp so both cards fit side-by-side
        h("div", { style: {
          fontSize: "12px", fontWeight: "600", color: "var(--du-ink-hi)",
          marginTop: "4px", lineHeight: "1.25",
          display: "-webkit-box", webkitLineClamp: "2",
          webkitBoxOrient: "vertical", overflow: "hidden",
          wordBreak: "break-word",
        }}, title),
        h("div", { style: {
          fontSize: "11px", color: "var(--du-ink-lo)", marginTop: "3px",
        }}, (w.stage_human || "Working…") + pages),
        h("div", { style: {
          marginTop: "6px", height: "4px", background: "var(--du-bg)",
          borderRadius: "999px", overflow: "hidden",
        }},
          h("div", { style: {
            height: "100%", width: pct + "%",
            background: "linear-gradient(90deg, var(--du-accent), var(--du-accent-2))",
            transition: "width 0.6s ease",
          }}),
        ),
      ));
    });

    $workers.appendChild(h("div", { class: "admin-section" },
      h("div", { style: {
        fontSize: "11px", fontWeight: "700", color: "var(--du-ink-lo)",
        textTransform: "uppercase", letterSpacing: "0.5px",
      }}, "Working now"),
      grid,
    ));
  }

  // ---- recent list: compact one-line rows, capped at 8 ----------------
  function renderList(items) {
    $list.innerHTML = "";
    const rows = items.slice(0, 8);
    if (!rows.length) {
      $list.appendChild(h("div", { class: "empty" },
        h("div", { class: "icon" }, "📭"),
        h("div", { class: "title" }, "No recent jobs"),
      ));
      return;
    }
    for (const j of rows) $list.appendChild(renderJob(j));
  }

  function renderJob(j) {
    const status = String(j.status || "").toLowerCase();
    const isDone = status === "done" || status === "completed";
    const isProcessing = status === "processing";
    const isFailed = status === "failed" || status.startsWith("failed");
    const dot = isDone ? "🟢" : isProcessing ? "🟡" : isFailed ? "🔴" : "⚪";
    const title = (j.title && String(j.title).trim())
      || (j.url ? String(j.url) : "")
      || ("#" + (j.id || j._id || ""));

    const open = (isDone || status === "partial") && j.open_link
      ? h("button", {
          class: "btn secondary",
          style: { padding: "4px 10px", fontSize: "11px", flexShrink: "0" },
          onClick: () => openLink(j.open_link),
        }, "🔗 Open")
      : null;

    return h("div", { class: "admin-section", style: { padding: "10px 12px" } },
      h("div", { style: { display: "flex", alignItems: "center", gap: "8px" } },
        h("span", { style: { flexShrink: "0" } }, dot),
        h("div", { style: {
          flex: "1", minWidth: 0, fontSize: "12px",
          color: "var(--du-ink-mid)", overflow: "hidden",
          textOverflow: "ellipsis", whiteSpace: "nowrap",
        }}, title),
        open,
      ),
      (isFailed && j.error_reason) ? h("div", { style: {
        fontSize: "11px", color: "var(--du-danger, #d33)", marginTop: "4px",
      }}, j.error_reason) : null,
    );
  }

  await tick();
  const t = setInterval(tick, POLL_MS);
  return () => clearInterval(t);
}
