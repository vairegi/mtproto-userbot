/*
  components.js — Component factory registry

  Every reusable UI atom is registered here.  Pages ONLY call components
  through this registry, never import them directly.  That lets you swap
  the implementation of e.g. "card" without touching every page that uses it.

  To replace a component:
    register("card", myNewCardFactory);
  To add a new one:
    register("banner", (props) => htmlNode);
  Both without touching any page.

  IMPORTANT — DO NOT put `import "components/*.js"` at the bottom of this
  file. Component files do `import { register } from "core/components.js"`
  which creates a circular graph, and their top-level `register(...)` call
  hits `factories` before this file finishes initializing (TDZ error on
  Android WebView). Component preloading lives in app.js instead, AFTER
  components.js has fully evaluated.
*/

// Lazy singleton so any accidental import cycle can't reference `factories`
// before it exists. `_factories()` returns the same Map every time.
let _map = null;
function _factories() {
  if (_map === null) _map = new Map();
  return _map;
}

export function register(name, factory) {
  _factories().set(name, factory);
}

export function make(name, props = {}) {
  const f = _factories().get(name);
  if (!f) throw new Error(`Component "${name}" not registered`);
  return f(props);
}

// Convenience helper for building DOM nodes quickly without JSX.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class")      el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "html")  el.innerHTML = v;
    else if (k.startsWith("on") && typeof v === "function") {
      el.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (k === "dataset" && typeof v === "object") {
      for (const [dk, dv] of Object.entries(v)) el.dataset[dk] = dv;
    } else {
      el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
