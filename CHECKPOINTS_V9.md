# CHECKPOINTS — V9 (Mini-app UI text refresh + Contact Admin + stylish buttons)

Task: SMALL grid (4 edits across 4 files, no new modules). Two checkpoints: 50% and 100%.

| %    | Description                                                                                     | File-wrapper URL                                       | AI Drive mirror                                              |
|------|-------------------------------------------------------------------------------------------------|--------------------------------------------------------|--------------------------------------------------------------|
| 50%  | 4 edits applied; bracket-balance green on all 3 edited JS files                                 | https://www.genspark.ai/api/files/s/nNy6gzjp           | `/DoujinshiUniverse_v2_checkpoints/FixPack_v9_50pct.zip`     |
| 100% | `verify_v2.sh` all 5 stages green — 43 assertions PASS. FINAL zip uploaded + mirrored.          | *(filled below at 100%)*                                | `/DoujinshiUniverse_v2_checkpoints/FixPack_v9_100pct.zip`    |

## Files changed (4)

1. `miniapp/frontend/js/plugins/card-actions.js` — UI text refresh
   * "📥 Queue to Channel" → **"📥 Download Now"** (both when NOT started AND
     when COMPLETED, since the underlying action is identical in both cases —
     deliver to DM). Icon changed from 🔗 to 📥 for the completed case to match.
   * "👁️ Preview First Pages" → **"👁️ Preview"**.
   * "⭐ Bookmark" → **"⭐ Save"**, and once tapped → **"⭐ Saved Already"**
     (dynamic label driven by the existing `store.get("bookmarks", [])`).
     The toggle behaviour is unchanged — tapping "Saved Already" removes the
     bookmark, just as tapping "Bookmark" used to. Toast text changed from
     "⭐ Bookmarked" to "⭐ Saved" to match.
   * **"🔗 Open on nhentai" action REMOVED** entirely (entry deleted from
     the `cardActions` array).

2. `miniapp/frontend/js/plugins/detail-sheet.js` — dynamic-label fix
   * The sheet previously interpolated `a.label` / `a.icon` into a template
     literal, which silently converted function values to their source text
     — a regression for any dynamic label. Now uses a small `resolve()`
     helper that invokes functions with the action ctx (gallery + me) and
     passes strings through unchanged. Also plumbs the optional
     `a.disabled(ctx)` predicate through so the sheet can disable buttons.

3. `miniapp/frontend/js/pages/settings.js` — Contact Admin
   * New **"Support"** section between "Data" and the footer with a single
     stylish button: **"💬 Contact Admin"**.
   * Tapping it opens `https://t.me/reportupdatesbot` via the existing
     `openLink()` helper from `core/telegram.js` (with a `window.open` fallback
     if `openLink` ever fails).
   * Sub-copy: *"Report a bug, request a feature, or ask the admin anything —
     opens @reportupdatesbot in Telegram."*
   * Haptic feedback on tap (`haptic("medium")`).

4. `miniapp/frontend/css/components.css` — stylish-button polish
   * All `.btn.primary` now uses a 135° gradient (accent → accent-darkened)
     with a soft 2-stop box-shadow, slight letter-spacing, hover lift
     (translateY(-1px) + brightness 1.06 + deeper shadow), and a press scale.
     Falls back to solid `--du-accent` when `color-mix()` isn't supported
     (the gradient's first stop provides the solid colour).
   * `.btn.secondary` gains a subtle hover state (background lift + deeper
     shadow + translateY(-1px)) so secondary buttons feel tactile too.
   * `.btn.danger` gets the same gradient + shadow treatment as `.primary`.
   * New `.btn-stylish` class (used by the Contact Admin button): larger
     padding, 3-stop gradient, inner top-highlight, and a hover "sheen sweep"
     (::after pseudo-element sliding across the button on hover). Tactile
     press state matches the others.

## UI text mapping (before → after)

| Before                       | After                                          |
|------------------------------|------------------------------------------------|
| 📥 Queue to Channel          | 📥 Download Now                                |
| 👁️ Preview First Pages       | 👁️ Preview                                     |
| ⭐ Bookmark                   | ⭐ Save  →  ⭐ Saved Already (after tap)        |
| 🔗 Open on nhentai           | *(removed)*                                    |
| —                            | 💬 Contact Admin (new, in Settings → Support) |

## What to verify after redeploy

1. **Download Now label:** Open any gallery in the Mini App. The primary
   button reads "📥 Download Now" (not "📥 Queue to Channel"). Tapping it
   behaves exactly as before — queues the gallery and DMs you when done.
   For an already-completed gallery the label is also "📥 Download Now"
   and the tap delivers instantly to your DM.
2. **Save / Saved Already:** Tap "⭐ Save" on a gallery → toast reads
   "⭐ Saved" → the button now reads "⭐ Saved Already". Tap again →
   toast "Removed from bookmarks" → button flips back to "⭐ Save".
3. **Preview label:** The preview button now reads "👁️ Preview" (shorter).
   Tapping still opens the preview modal as before.
4. **Open on nhentai removed:** The detail sheet now shows only three
   buttons: Download Now / Preview / Save. The "🔗 Open on nhentai" button
   is gone.
5. **Contact Admin:** Settings tab → scroll to bottom → "Support" section.
   Tap "💬 Contact Admin" → Telegram opens the DM with @reportupdatesbot.
   The button has a gradient + sheen sweep on hover.
6. **Stylish buttons:** All primary/danger buttons across the app now have
   a gradient + hover-lift + shadow. Secondary buttons have a subtle
   background lift on hover.
