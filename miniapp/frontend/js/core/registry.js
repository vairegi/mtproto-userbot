/*
  registry.js — Page & tab-bar registration

  THIS is the file you edit to add/remove/reorder pages.

  Each entry is a self-contained page module.  Adding a new tab:
    1. Create frontend/js/pages/<name>.js exporting { id, title, render, onEnter, onLeave }.
    2. Import + register it below.
    3. Done.  Nothing else in the app needs to change.

  Each entry:
    id       -> unique key, also used as URL hash (#search, #bookmarks, ...)
    title    -> header title when this tab is active
    icon     -> emoji or single-char label for the tab bar
    label    -> tab-bar label
    module   -> dynamic import so pages load lazily
    adminOnly-> if true, hidden unless backend confirms the caller is admin
*/

export const pages = [
  {
    id: "search",
    title: "Discover",
    icon: "🔎",
    label: "Search",
    module: () => import("pages/search.js"),
  },
  {
    id: "bookmarks",
    title: "Bookmarks",
    icon: "⭐",
    label: "Saved",
    module: () => import("pages/bookmarks.js"),
  },
  {
    id: "queue",
    title: "Queue",
    icon: "📥",
    label: "Queue",
    module: () => import("pages/queue.js"),
  },
  {
    id: "profile",
    title: "Profile",
    icon: "👤",
    label: "Profile",
    module: () => import("pages/profile.js"),
  },
  {
    id: "settings",
    title: "Settings",
    icon: "🎛️",
    label: "Settings",
    module: () => import("pages/settings.js"),
  },
  {
    id: "admin",
    title: "Admin",
    icon: "⚙️",
    label: "Admin",
    module: () => import("pages/admin.js"),
    adminOnly: true,
  },
];

export function findPage(id) {
  return pages.find(p => p.id === id) || pages[0];
}
