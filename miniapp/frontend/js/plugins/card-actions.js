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
import { openPreview } from "plugins/preview-modal.js";

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

// -- Actions -----------------------------------------------------------------

export const cardActions = [
  {
    id: "queue_or_open",
    // Dynamic label + icon: shows "Open Post" for known galleries, a
    // disabled "Downloading…" while in flight, and "Queue to Channel"
    // for everything else.
    label: ({ gallery }) => {
      if (isCompleted(gallery))  return "Open Post";
      if (isProcessing(gallery)) return "Downloading…";
      return "Queue to Channel";
    },
    icon: ({ gallery }) => {
      if (isCompleted(gallery))  return "🔗";
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
      // --- Already on file → jump straight to the DB channel post.
      if (isCompleted(gallery)) {
        const link = openLinkOf(gallery);
        if (link) {
          openLink(link);
          close && close();
          return;
        }
        // Rare: server says COMPLETED but has no open_link. Fall through to
        // enqueue so relay_v2 can re-post; the dedup gate is idempotent.
      }

      // --- Currently downloading → tell the user, don't spam.
      if (isProcessing(gallery)) {
        toast("Already downloading — hang tight", "");
        return;
      }

      // --- Normal enqueue path (relay_v2 will de-dup at the server too).
      try {
        const url = `https://nhentai.net/g/${gallery.id}/`;
        const res = await api.post("/api/queue", { url });

        // Server-side dedup can still fire between our /status peek and
        // this POST (e.g. someone else queued the same gallery). Honour
        // whatever the server just said.
        if (res && res.deduped) {
          if (res.action === "already_completed" && res.open_link) {
            gallery.v2_status = {
              known: true,
              status: res.status || "COMPLETED",
              open_link: res.open_link,
              title: res.title,
            };
            openLink(res.open_link);
            toast("Already in the library — opened", "success");
            close && close();
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
    },
  },
  {
    id: "preview",
    label: "Preview First Pages",
    icon: "👁️",
    kind: "secondary",
    run({ gallery }) { openPreview(gallery); },
  },
  {
    id: "bookmark",
    label: "Bookmark",
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
          toast("⭐ Bookmarked", "success");
        }
      } catch (e) { toast("Failed: " + e.message, "error"); }
    },
  },
  {
    id: "open_source",
    label: "Open on nhentai",
    icon: "🔗",
    kind: "secondary",
    run({ gallery }) {
      openLink(`https://nhentai.net/g/${gallery.id}/`);
    },
  },
];
