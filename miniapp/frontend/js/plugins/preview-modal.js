/*
  preview-modal.js — First-page-peek preview for a gallery.

  Opens a lightweight image viewer showing the first 3-4 pages of a gallery
  directly from nhentai's public image CDN. Lets the user vet a gallery
  before queueing it.

  Wired into card-actions as an entry — if you want to remove the "Preview"
  button, just delete that entry in card-actions.js. This module can stay.

  Usage:
    import { openPreview } from "plugins/preview-modal.js";
    openPreview(gallery);   // gallery must have .id and .cover
*/

import { make, h } from "core/components.js";
import { api } from "core/api.js";
import { haptic } from "core/telegram.js";

const CDN_HOSTS = [
  "https://i.nhentai.net",
  "https://i1.nhentai.net",
  "https://i2.nhentai.net",
  "https://i3.nhentai.net",
  "https://i4.nhentai.net",
];

// nhentai media-id lives inside the cover URL like  .../galleries/<mid>/cover.jpg
function extractMediaId(coverUrl) {
  if (!coverUrl) return null;
  const m = coverUrl.match(/\/galleries\/(\d+)\//);
  return m ? m[1] : null;
}

// Extension token → filename ext
const EXT_MAP = { j: "jpg", p: "png", g: "gif", w: "webp" };

export async function openPreview(gallery) {
  const spinner = h("div", { class: "empty" },
    h("div", { class: "icon" }, "⏳"),
    h("div", { class: "title" }, "Loading preview…"),
  );
  const sheet = make("sheet", { title: "Preview", body: spinner });
  sheet.open();

  // Try to get richer detail (page list with per-page extensions).
  let detail = null;
  try { detail = await api.get(`/api/gallery/${gallery.id}`); }
  catch (_) { /* fall through to CDN guess */ }

  const mediaId = extractMediaId(detail?.cover || gallery.cover);
  if (!mediaId) {
    spinner.innerHTML = "";
    spinner.appendChild(h("div", { class: "icon" }, "⚠️"));
    spinner.appendChild(h("div", { class: "title" }, "Preview unavailable"));
    spinner.appendChild(h("div", {}, "Could not resolve media id."));
    return;
  }

  // Build URLs for the first 4 pages. We don't know the exact per-page
  // extension without hitting /api/gallery/{id} on nhentai, so we try .jpg
  // and let onerror fall back to .png/.webp.
  const pageCount = Math.min(4, gallery.pages || 4);
  const urls = [];
  for (let i = 1; i <= pageCount; i++) {
    urls.push(`${CDN_HOSTS[0]}/galleries/${mediaId}/${i}.jpg`);
  }

  const viewer = h("div", {
    style: {
      display: "flex", overflowX: "auto", gap: "12px",
      scrollSnapType: "x mandatory", paddingBottom: "8px",
    },
  });
  for (const u of urls) {
    const img = h("img", {
      src: u,
      loading: "lazy",
      style: {
        flex: "0 0 auto",
        width: "80%", maxWidth: "320px",
        aspectRatio: "2 / 3",
        borderRadius: "12px",
        objectFit: "cover",
        scrollSnapAlign: "center",
        background: "var(--du-bg-2)",
      },
    });
    // Simple ext fallback: jpg → png → webp
    let tried = 0;
    img.addEventListener("error", () => {
      const exts = ["png", "webp"];
      if (tried >= exts.length) return;
      img.src = img.src.replace(/\.\w+$/, "." + exts[tried++]);
    });
    viewer.appendChild(img);
  }

  spinner.replaceWith(viewer);
  haptic("light");
}
