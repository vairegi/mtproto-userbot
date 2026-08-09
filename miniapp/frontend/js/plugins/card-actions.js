/*
  card-actions.js — Which action buttons appear on a gallery's detail sheet

  ⭐ THIS IS THE FILE TO EDIT WHEN ADDING/REMOVING ACTIONS ⭐

  Every button on the gallery detail sheet is just an entry in this array.
  Add / remove / reorder without touching the card component, the search
  page, or anything else.

  Each action:
    id         unique key (used for admin permission checks)
    label      button text (may be a function of ctx for dynamic labels)
    icon       emoji prefix (may be a function of ctx)
    kind       "primary" | "secondary" | "danger"
    when(ctx)  optional predicate: return false to hide.  ctx = { gallery, me }
    run(ctx)   handler.  ctx = { gallery, me, api, toast, close }

  Add a new button? Push into the array below.
  Remove one? Delete or comment out its entry.

  V2 note:
    The primary "Queue to Channel" action auto-swaps to "Open Post" when
    the backend's V2 dedup state (embedded in gallery.v2_status by
    /api/gallery/{id}) says the gallery is already COMPLETED or PARTIAL,
    and disables itself when PROCESSING. See docs/ARCHITECTURE_V2.md.
*/

import { api } from "core/api.js";
import { make } from "core/components.js";
import { openLink } from "core/telegram.js";
import { store } from "core/state.js";
// v12.3: preview-modal import REMOVED — the Preview action is gone entirely
// (admin decision: preview + its thumbnail fetches fed the 429 storm).
// v11 hotfix (v11.1): this import was dropped in the v11 100% ship, which
// made every "Download Now" click throw ReferenceError: showActionLoader is
// not defined — silently swallowed by the async run() so nothing at all
// happened on the UI and no POST /api/queue was ever fired. Restored here.
import { showActionLoader, hideActionLoader } from "ui/action-loader.js";

const toast = (text, kind) => make("toast", { text, kind });

// -- V2 status helpers --------------------------------------------------------

function v2Of(gallery) {
  return (gallery && gallery.v2_status) || {};
}

function isCompleted(gallery) {
  const s = (v2Of(gallery).status || "").toUpperCase();
  return v2Of(gallery).known && (s === "COMPLETED" || s === "PARTIAL");
}

function isProcessing(gallery) {
  return (v2Of(gallery).status || "").toUpperCase() === "PROCESSING";
}

function openLinkOf(gallery) {
  return v2Of(gallery).open_link || "";
}

// -- v11.8 (#5): shared save-count cache + pretty formatter ----------------
const _saveCountCache = new Map();

function _fmtCount(n) {
  n = Number(n) || 0;
  if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "k";
  return String(n);
}

// Fire-and-forget warmer — called from the sheet-open path so by the time
// the user reads the action row the count is already cached.
export async function warmSaveCount(galleryId) {
  const key = String(galleryId || "");
  if (!key || _saveCountCache.has(key)) return;
  try {
    const r = await api.get(`/api/bookmarks/count/${encodeURIComponent(key)}`);
    _saveCountCache.set(key, Number(r.saves) || 0);
  } catch (_) { /* ignore — label will render without count */ }
}

// -- Actions -----------------------------------------------------------------

export const cardActions = [
  {
    id: "queue_or_open",
    // Dynamic label + icon: shows "Open Post" for known galleries, a
    // disabled "Downloading…" while in flight, and "Queue to Channel"
    // for everything else.
    label: ({ gallery }) => {
      if (isCompleted(gallery))  return "Download Now";
      if (isProcessing(gallery)) return "Downloading…";
      return "Download Now";
    },
    icon: ({ gallery }) => {
      if (isCompleted(gallery))  return "📥";
      if (isProcessing(gallery)) return "⏳";
      return "📥";
    },
    kind: "primary",
    when: ({ me, gallery }) => {
      // Backend can still hard-disable queueing per user.
      if (me && me.can_queue === false && !isCompleted(gallery)) return false;
      return true;
    },
    disabled: ({ gallery }) => isProcessing(gallery),
    async run({ gallery, close }) {
      // v11: paint the hourglass overlay IMMEDIATELY so the click feels
      // accepted in <50 ms. Every code path below releases the token via
      // the outer try/finally (both primary and swap tokens).
      const _ldr = showActionLoader(
        isCompleted(gallery) ? "Sending to your DM…" : "Queuing…"
      );
      // Secondary token used only to swap the label between phases.
      // Kept as `let` so we can rebind it after each phase transition.
      let _ldr_swap = _ldr;
      try {
      // --- Already on file → DM the user the cover + PDF via the admin bot.
      // BUG 1 fix: previously this called openLink(t.me/c/<internal>/<msg>)
      // which just jumped the user to the channel. Now we POST to the new
      // /api/queue/deliver/<id> endpoint, which uses Bot API copyMessage
      // to forward the cover + PDF straight into the user's DM.
      if (isCompleted(gallery)) {
        try {
          await api.post(`/api/queue/deliver/${gallery.id}`, {});
          toast("📨 Sent to your DM", "success");
          close && close();
          return;
        } catch (e) {
          // Fall through to the normal enqueue path only when the gallery
          // isn't in the library yet. Everything else is a real delivery
          // failure — surface the reason instead of silently re-queuing.
          if (e && e.status === 404) {
            toast("Not in library yet — queuing…", "");
          } else {
            const msg = (e && (e.message || e.detail)) || "unknown";
            // Feature 3 (force-join): backend may surface a join-required
            // block as a 200 with blocked_by_force_join — but if it ever
            // arrives as an error, handle it here too.
            if (/join the required|force.?join/i.test(msg)) {
              toast("🔒 Join the required channel(s) — check your DM", "");
            } else if (/initiate conversation|\/start/i.test(msg)) {
              toast("👋 Send /start to the bot in DM first, then try again.", "error");
            } else {
              toast("DM delivery failed: " + msg, "error");
            }
            return;
          }
        }
      }

      // --- Currently downloading → tell the user, don't spam.
      if (isProcessing(gallery)) {
        toast("Already downloading — hang tight", "");
        return;
      }

      // v11: swap the overlay label — we're about to hit /api/queue,
      // which is the slow leg (3–4 s in the field).
      hideActionLoader(_ldr_swap);
      _ldr_swap = showActionLoader("Queuing your download…");

      // --- Normal enqueue path (relay_v2 will de-dup at the server too).
      try {
        const url = `https://nhentai.net/g/${gallery.id}/`;
        const res = await api.post("/api/queue", { url });

        // Server-side dedup can still fire between our /status peek and
        // this POST (e.g. someone else queued the same gallery). Honour
        // whatever the server just said.
        if (res && res.deduped) {
          if (res.action === "already_completed") {
            // BUG 1 fix: the backend already tried copyMessage on our
            // behalf. Reflect what actually happened instead of opening
            // a channel link.
            gallery.v2_status = {
              known: true,
              status: res.status || "COMPLETED",
              title: res.title,
            };
            if (res.delivered) {
              toast(res.message || "📨 Sent to your DM", "success");
              close && close();
            } else if (res.blocked_by_force_join) {
              // Feature 3 (force-join): the bot already DM'd a 'please join'
              // prompt with Join buttons + an 'I've joined' callback.
              toast(res.message || "🔒 Join the required channel(s) — check your DM", "");
            } else {
              const errMsg = res.delivery_error || res.message || "DM delivery failed";
              if (/initiate conversation|\/start/i.test(errMsg)) {
                toast("👋 Send /start to the bot in DM first, then try again.", "error");
              } else {
                toast(errMsg, "error");
              }
            }
            return;
          }
          if (res.action === "already_processing") {
            gallery.v2_status = {
              known: true,
              status: "PROCESSING",
              title: res.title,
            };
            toast("Already downloading — hang tight", "");
            return;
          }
        }

        // v11.5 fix: the empty_result branch used to be nested INSIDE the
        // `if (res.deduped)` block above, but empty_result responses have
        // deduped:false — so this handler was unreachable and the code
        // fell through to a false-positive "✅ Queued" toast. Handling it
        // at the correct scope now: any ok:false soft-fail from the
        // backend must NOT be shown as a success.
        if (res && res.ok === false) {
          const msg = res.message
            || (res.action === "empty_result"
                  ? "This URL couldn't be queued right now — ask an admin to Force Re-scrape it."
                  : ("Failed: " + (res.reason || res.action || "unknown")));
          toast(msg, "error");
          return;
        }

        // Fresh enqueue succeeded.
        toast("✅ Queued #" + gallery.id, "success");
        close && close();
      } catch (e) {
        if (e && e.status === 429) {
          toast("⏳ Rate limit — try again later", "error");
        } else if (e && e.status === 403) {
          toast("App is private — admins only right now", "error");
        } else {
          toast("Failed: " + ((e && e.message) || "unknown"), "error");
        }
      }
      } finally {
        // v11: always release the loader — both primary and swap tokens.
        // hideActionLoader() is a no-op on unknown / already-released ids,
        // so releasing both is safe even when they refer to the same overlay.
        hideActionLoader(_ldr_swap);
        hideActionLoader(_ldr);
      }
    },
  },
  {
    // v12.3: the "preview" action that used to live here is removed
    // entirely. Sheet actions are now: Download Now, Save, Share.
    id: "bookmark",
    // UI text v9: was "Bookmark" — now "Save", flipping to "Saved Already"
    // when the gallery is already in the user's bookmarks.
    // v11.8 (#5): label ALSO shows the global save count from the backend,
    // fetched on-demand when the action is rendered. Reads from a tiny
    // module-level cache so re-renders don't re-hit the API.
    label: ({ gallery }) => {
      const cur = store.get("bookmarks", []);
      const mine = cur.some(b => String(b.id) === String(gallery.id));
      const cached = _saveCountCache.get(String(gallery.id));
      const suffix = (cached != null && cached > 0)
        ? ` · ${_fmtCount(cached)}`
        : "";
      return (mine ? "Saved Already" : "Save") + suffix;
    },
    icon: "⭐",
    kind: "secondary",
    async run({ gallery }) {
      try {
        const cur = store.get("bookmarks", []);
        const already = cur.some(b => String(b.id) === String(gallery.id));
        if (already) {
          await api.del("/api/bookmarks/" + gallery.id);
          store.set("bookmarks", cur.filter(b => String(b.id) !== String(gallery.id)));
          toast("Removed from bookmarks", "");
        } else {
          await api.post("/api/bookmarks", {
            id: gallery.id, title: gallery.title, cover: gallery.cover,
            pages: gallery.pages,
          });
          store.set("bookmarks", [...cur, gallery]);
          toast("⭐ Saved", "success");
        }
      } catch (e) { toast("Failed: " + e.message, "error"); }
    },
  },
  // v11.7 — Share button. Copies a Telegram deep-link so the recipient
  // opens the mini-app directly on this gallery.
  {
    id: "share",
    label: "Share",
    icon:  "🔗",
    kind:  "secondary",
    async run({ gallery, close }) {
      const gid = String(gallery.id || "");
      if (!gid) { toast("Nothing to share", "error"); return; }

      // Prefer t.me/<bot>/app?startapp=g_<id> when running inside Telegram;
      // fall back to a hash-URL for external browsers.
      let shareUrl = "";
      try {
        const tg = window.Telegram && window.Telegram.WebApp;
        const botUser =
          tg?.initDataUnsafe?.bot?.username ||
          window.__DU_BOT_USERNAME__;
        if (botUser) {
          shareUrl = `https://t.me/${botUser}/app?startapp=g_${gid}`;
        } else {
          const base = window.location.href.split("#")[0];
          shareUrl = `${base}#/gallery/${gid}`;
        }
      } catch (_) {
        shareUrl = `${window.location.href.split("#")[0]}#/gallery/${gid}`;
      }

      // Native share sheet (mobile) first, clipboard second, prompt last.
      let delivered = false;
      try {
        if (navigator.share) {
          await navigator.share({
            title: gallery.title || `Gallery #${gid}`,
            text:  gallery.title || "",
            url:   shareUrl,
          });
          delivered = true;
        } else if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(shareUrl);
          toast("🔗 Link copied", "success");
          delivered = true;
        }
      } catch (_) { /* user cancelled */ }
      if (!delivered) {
        try { window.prompt("Copy this link:", shareUrl); } catch (_) {}
      }

      // Telemetry for the Sharer badge — fire-and-forget.
      try { await api.post("/api/stats/share", { gallery_id: gid }); }
      catch (_) { /* ignore */ }

      close && close();
    },
  },
];
