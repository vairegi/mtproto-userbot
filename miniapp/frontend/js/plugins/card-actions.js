/*
  card-actions.js — Which action buttons appear on a gallery's detail sheet

  ⭐ THIS IS THE FILE TO EDIT WHEN ADDING/REMOVING ACTIONS ⭐

  Every button on the gallery detail sheet is just an entry in this array.
  Add / remove / reorder without touching the card component, the search
  page, or anything else.

  Each action:
    id         unique key (used for admin permission checks)
    label      button text
    icon       emoji prefix
    kind       "primary" | "secondary" | "danger"
    when(ctx)  optional predicate: return false to hide.  ctx = { gallery, me }
    run(ctx)   handler.  ctx = { gallery, me, api, toast, close }

  Add a new button? Push into the array below.
  Remove one? Delete or comment out its entry.
*/

import { api } from "core/api.js";
import { make } from "core/components.js";
import { openLink } from "core/telegram.js";
import { store } from "core/state.js";
import { openPreview } from "plugins/preview-modal.js";

const toast = (text, kind) => make("toast", { text, kind });

export const cardActions = [
  {
    id: "queue",
    label: "Queue to Channel",
    icon: "📥",
    kind: "primary",
    when: ({ me }) => me.can_queue !== false,   // backend can disable it
    async run({ gallery, close }) {
      try {
        const url = `https://nhentai.net/g/${gallery.id}/`;
        await api.post("/api/queue", { url });
        toast("✅ Queued #" + gallery.id, "success");
        close && close();
      } catch (e) {
        if (e.status === 429) {
          toast("⏳ Rate limit — try again later", "error");
        } else {
          toast("Failed: " + (e.message || "unknown"), "error");
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
