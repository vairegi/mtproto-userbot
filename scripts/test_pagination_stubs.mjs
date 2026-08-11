/*
  test_pagination_stubs.mjs — stub modules + DOM shim for the browserless
  search.js pagination test (v12.16). The test runner reads the REAL
  miniapp/frontend/js/pages/search.js, rewrites ONLY its bare import
  specifiers ("core/api.js", "plugins/...", ...) to this file's stub
  exports, writes the transformed copy to a temp file, and imports it —
  so the logic under test is byte-for-byte the production file.
*/

/* ---------------- minimal DOM ---------------- */

class StubNode {
  constructor() { this.childNodes = []; this.parentNode = null; }
  appendChild(c) {
    if (c == null) return c;
    if (c.parentNode) c.parentNode.removeChild(c);
    this.childNodes.push(c); c.parentNode = this; return c;
  }
  append(...cs) { for (const c of cs) this.appendChild(typeof c === "string" ? new StubText(c) : c); }
  removeChild(c) { const i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null; return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get children() { return this.childNodes.filter(c => c.nodeType === 1); }
  get childElementCount() { return this.children.length; }
  querySelector(sel) { return this._find(sel, false)[0] || null; }
  querySelectorAll(sel) { return this._find(sel, true); }
  _find(sel, all) {
    const out = [];
    const wantClass = sel.startsWith(".") ? sel.slice(1) : null;
    const walk = (n) => {
      for (const c of n.childNodes) {
        if (c.nodeType === 1) {
          const cls = (c.className || "");
          const tag = (c.tagName || "").toLowerCase();
          if ((wantClass && cls.split(/\s+/).includes(wantClass)) || (!wantClass && tag === sel.toLowerCase())) {
            out.push(c); if (!all) return true;
          }
          if (walk(c)) return true;
        }
      }
      return false;
    };
    walk(this);
    return out;
  }
  get textContent() { return this.childNodes.map(c => c.textContent).join(""); }
  set textContent(v) {
    this.childNodes = [];
    if (v !== "" && v != null) this.childNodes.push(new StubText(String(v)));
  }
  set innerHTML(_v) { this.childNodes = []; }
  get innerHTML() { return ""; }
}

class StubText {
  constructor(t) { this.nodeType = 3; this._t = t; this.parentNode = null; }
  get textContent() { return this._t; }
  set textContent(v) { this._t = v; }
}

class StubElement extends StubNode {
  constructor(tag) {
    super();
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.className = "";
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.listeners = {};
    this.disabled = false;
    this.value = "";
    this.classList = {
      add: (...cs) => { const s = new Set((this.className || "").split(/\s+/).filter(Boolean)); cs.forEach(c => s.add(c)); this.className = [...s].join(" "); },
      remove: (...cs) => { const s = new Set((this.className || "").split(/\s+/).filter(Boolean)); cs.forEach(c => s.delete(c)); this.className = [...s].join(" "); },
      toggle: (c, force) => {
        const s = new Set((this.className || "").split(/\s+/).filter(Boolean));
        const want = force === undefined ? !s.has(c) : !!force;
        if (want) s.add(c); else s.delete(c);
        this.className = [...s].join(" ");
        return want;
      },
      contains: (c) => (this.className || "").split(/\s+/).includes(c),
    };
  }
  setAttribute(k, v) { this.attributes[k] = String(v); if (k === "class") this.className = String(v); }
  getAttribute(k) { return this.attributes[k] ?? null; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener(t, fn) { (this.listeners[t] = this.listeners[t] || []).push(fn); }
  removeEventListener(t, fn) { const a = this.listeners[t]; if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); } }
  dispatchEvent(ev) { for (const fn of this.listeners[ev.type] || []) fn.call(this, ev); return true; }
  click() { this.dispatchEvent({ type: "click", target: this, preventDefault() {} }); }
  blur() {}
}

/* ---------------- globals ---------------- */

const listeners = {};
const locationObj = { hash: "" };
const historyObj = {
  replaceState(_s, _t, url) {
    const u = String(url);
    locationObj.hash = u.startsWith("#") ? u : "#" + u;
  },
};

globalThis.Node = StubNode;
globalThis.document = {
  createElement: (t) => new StubElement(t),
  createTextNode: (t) => new StubText(String(t)),
  getElementById: () => null,
  documentElement: new StubElement("html"),
  body: new StubElement("body"),
};
globalThis.window = {
  addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
  removeEventListener: (t, fn) => { const a = listeners[t]; if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); } },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  Telegram: undefined,
};
globalThis.location = locationObj;
globalThis.history = historyObj;

export const __test = {
  fireHashChange: () => { for (const fn of listeners.hashchange || []) fn({ type: "hashchange" }); },
  getHash: () => locationObj.hash,
  setHash: (h) => { locationObj.hash = h; },
  StubElement,
};

/* ---------------- stub modules ---------------- */

export const __apiCalls = [];
let __pages = {};   // page -> { items, has_more }
export function __setPages(p) { __pages = p; __apiCalls.length = 0; }

export const api = {
  async get(path, params) {
    if (path === "/api/search") {
      __apiCalls.push({ page: params.page, sort: params.sort, q: params.q, per_page: params.per_page });
      const pg = __pages[params.page] || { items: [], has_more: false };
      return {
        items: pg.items, page: params.page, per_page: params.per_page,
        has_more: pg.has_more, upstream_rate_limited: false,
      };
    }
    return {};
  },
  async post() { return {}; },
};

export function h(tag, attrs = {}, ...children) {
  const el = new StubElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.appendChild(c instanceof StubNode ? c : new StubText(String(c)));
  }
  return el;
}

export function make(name, props = {}) {
  if (name === "skeleton") { const s = new StubElement("div"); s.className = "skeleton-stub"; return s; }
  if (name === "chip") {
    const c = new StubElement("button"); c.className = "chip";
    c.appendChild(new StubText(props.label || ""));
    if (props.onChange) c.addEventListener("click", props.onChange);
    return c;
  }
  if (name === "card") {
    const c = new StubElement("div"); c.className = "card";
    c.dataset.galleryId = String(props.id);
    c.appendChild(new StubText(props.title || ""));
    return c;
  }
  if (name === "sheet") return { open() {}, close() {} };
  if (name === "toast") return new StubElement("div");
  return new StubElement("div");
}

export const haptic = () => {};
export const store = { get: () => null, set: () => {} };

export function parseSearch(q) {
  // minimal: honor "sort:xxx" operator, pass the rest through as q
  let sort = "";
  const m = String(q || "").match(/(?:^|\s)sort:([a-z-]+)/i);
  if (m) sort = m[1].toLowerCase();
  return { q: String(q || ""), include_tags: [], exclude_tags: [], artist: "", pages_min: null, pages_max: null, sort, lang: "" };
}

export const cardActions = [];

export const renderTrendingTags = () => new StubElement("div");
export const renderRecommendations = () => new StubElement("div");
