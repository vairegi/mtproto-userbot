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
*/

const factories = new Map();

export function register(name, factory) {
  factories.set(name, factory);
}

export function make(name, props = {}) {
  const f = factories.get(name);
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

// Pre-register the built-in components so pages can use them from turn 1.
// Each import registers itself as a side-effect.
import "components/card.js";
import "components/chip.js";
import "components/sheet.js";
import "components/toast.js";
import "components/skeleton.js";
