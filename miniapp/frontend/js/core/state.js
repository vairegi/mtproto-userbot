/*
  state.js — Tiny reactive store (no framework)

  Usage:
    import { store } from "core/state.js";
    const unsub = store.subscribe("bookmarks", (val) => renderBookmarks(val));
    store.set("bookmarks", [...store.get("bookmarks"), newItem]);

  Keeps state global so pages can share (e.g. queue badge sees a queue push
  from the search page). Small enough to read in one sitting.
*/

const state = new Map();
const subs = new Map();

export const store = {
  get(key, fallback = undefined) {
    return state.has(key) ? state.get(key) : fallback;
  },
  set(key, value) {
    state.set(key, value);
    const listeners = subs.get(key);
    if (listeners) for (const fn of listeners) { try { fn(value); } catch (_) {} }
  },
  update(key, fn, fallback = undefined) {
    const cur = state.has(key) ? state.get(key) : fallback;
    this.set(key, fn(cur));
  },
  subscribe(key, fn) {
    if (!subs.has(key)) subs.set(key, new Set());
    subs.get(key).add(fn);
    return () => subs.get(key).delete(fn);
  },
};
