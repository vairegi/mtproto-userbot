/* ============================================================================
 * ui/action-loader.js — v11
 *
 * Full-screen hourglass overlay that any action can pop up INSTANTLY on
 * click while an async request is in flight.
 *
 * Why this exists:
 *   The old "Download Now" button awaited POST /api/queue → the whole
 *   round-trip took 3–4s during which nothing changed on screen. Users
 *   reported it "feels stuck". This module lets the click handler paint
 *   a loader in <50 ms, before the network call even starts, so the
 *   click always feels accepted.
 *
 * API:
 *   const token = showActionLoader('Queuing…');   // paints immediately
 *   try { await api.post(...); } finally { hideActionLoader(token); }
 *
 * Multiple concurrent showActionLoader() calls are ref-counted: the
 * overlay stays visible until every token has been hidden. The label
 * shown is the label of the MOST RECENT active token.
 * ==========================================================================*/

// SVG markup for the Uiverse hourglass (the classes are styled by
// css/loader-hourglass.css — do not rename them).
const HOURGLASS_SVG = `
<svg class="loader" role="img" aria-label="Loading"
     viewBox="0 0 52 52" xmlns="http://www.w3.org/2000/svg">
  <g fill="none" stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5">
    <!-- Motion rings -->
    <circle class="loader__motion-thin"   cx="26" cy="26" r="24.5"
            stroke="rgba(255,255,255,0)" stroke-dasharray="153.94"/>
    <circle class="loader__motion-medium" cx="26" cy="26" r="24.5"
            stroke="rgba(255,255,255,0)" stroke-dasharray="153.94"/>
    <circle class="loader__motion-thick"  cx="26" cy="26" r="24.5"
            stroke="rgba(255,255,255,0)" stroke-dasharray="153.94" stroke-width="2.5"/>
    <!-- Hourglass model (rotates 180° each cycle) -->
    <g class="loader__model">
      <!-- Frame -->
      <path d="M0.5 0.5 h24.5 M0.5 31.5 h24.5" stroke="#f6d38a"/>
      <path d="M2 0.5 v3 c0 6 9 10 10.25 12 c1.25 -2 10.25 -6 10.25 -12 v-3"
            stroke="#f6d38a"/>
      <path d="M2 31.5 v-3 c0 -6 9 -10 10.25 -12 c1.25 2 10.25 6 10.25 12 v3"
            stroke="#f6d38a"/>
      <!-- Glare highlights -->
      <path class="loader__glare-top"    d="M4.25 2.5 v1 c0 4 6 7 8 8.5"
            stroke="white"/>
      <path class="loader__glare-bottom" d="M4.25 29.5 v-1 c0 -4 6 -7 8 -8.5"
            stroke="rgba(255,255,255,0)"/>
      <!-- Sand -->
      <path class="loader__sand-mound-top"
            d="M4.5 15.5 c1.5 -2 6 -3 7.75 -3 c1.75 0 6.25 1 7.75 3"
            stroke="#f6d38a"/>
      <path class="loader__sand-mound-bottom"
            d="M4.5 31.5 c1.5 -3 6 -5 7.75 -5 c1.75 0 6.25 2 7.75 5"
            stroke="#f6d38a"/>
      <path class="loader__sand-drop"       d="M12.25 15.5 v13"
            stroke="#f6d38a" stroke-dasharray="108"/>
      <path class="loader__sand-fill"       d="M12.25 15.5 v13"
            stroke="#f6d38a" stroke-dasharray="55"/>
      <path class="loader__sand-line-left"  d="M6 30.75 l4 -6"
            stroke="#f6d38a" stroke-dasharray="55"/>
      <path class="loader__sand-line-right" d="M18.5 30.75 l-4 -6"
            stroke="#f6d38a" stroke-dasharray="25"/>
      <path class="loader__sand-grain-left"  d="M10 27.5 l-2 3.25"
            stroke="#f6d38a" stroke-dasharray="30"/>
      <path class="loader__sand-grain-right" d="M14.5 27.5 l2 3.25"
            stroke="#f6d38a" stroke-dasharray="27"/>
    </g>
  </g>
</svg>`;

// Active tokens, in insertion order. The topmost label is shown.
const _tokens = new Map();  // id -> label
let   _nextTokenId = 1;
let   _overlayEl   = null;
let   _labelEl     = null;
let   _leaveTimer  = null;

function _ensureOverlay() {
  if (_overlayEl && document.body.contains(_overlayEl)) return _overlayEl;

  const el = document.createElement("div");
  el.className = "loader-overlay";
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  el.innerHTML = `
    <div class="loader-overlay__box">
      ${HOURGLASS_SVG}
      <div class="loader-overlay__label"></div>
    </div>`;
  document.body.appendChild(el);
  _overlayEl = el;
  _labelEl   = el.querySelector(".loader-overlay__label");
  return el;
}

function _refreshLabel() {
  if (!_labelEl) return;
  // Show the most recently added token's label.
  let last = "";
  for (const v of _tokens.values()) last = v;
  _labelEl.textContent = last || "";
}

/**
 * Paint the hourglass overlay immediately.
 * @param {string} [label] - user-visible message (e.g. "Queuing…")
 * @returns {number} token — pass to hideActionLoader() when done.
 */
export function showActionLoader(label = "Loading…") {
  // Cancel any in-flight fade-out; we're staying visible.
  if (_leaveTimer) { clearTimeout(_leaveTimer); _leaveTimer = null; }
  if (_overlayEl)  _overlayEl.classList.remove("is-leaving");

  const id = _nextTokenId++;
  _tokens.set(id, String(label || ""));
  _ensureOverlay();
  _refreshLabel();
  return id;
}

/**
 * Hide the overlay for one token. Overlay only vanishes when every
 * outstanding token has been released.
 */
export function hideActionLoader(token) {
  if (token == null) return;
  _tokens.delete(token);
  if (_tokens.size > 0) { _refreshLabel(); return; }

  // Last token gone → fade out and remove.
  if (!_overlayEl) return;
  _overlayEl.classList.add("is-leaving");
  const el = _overlayEl;
  _leaveTimer = setTimeout(() => {
    if (el && el.parentNode) el.parentNode.removeChild(el);
    if (_overlayEl === el) { _overlayEl = null; _labelEl = null; }
    _leaveTimer = null;
  }, 160);
}

/* ============================================================================
 * v12.13 (#B): compact inline indicator
 * ----------------------------------------------------------------------------
 * The full-screen hourglass is heavy for the fast DB→DM path (usually <1s).
 * showInlineLoader() paints a small, non-blocking pill in the corner of the
 * viewport instead. It shares no state with the full-screen overlay so both
 * can coexist during transitions. Token API mirrors show/hideActionLoader.
 * ==========================================================================*/
const _inlineTokens = new Map();
let   _inlineNextId = 1;
let   _inlineEl     = null;
let   _inlineLabel  = null;
let   _inlineLeave  = null;

function _ensureInline() {
  if (_inlineEl && document.body.contains(_inlineEl)) return _inlineEl;
  const el = document.createElement("div");
  el.className = "inline-loader";
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  el.innerHTML = `
    <span class="inline-loader__spinner" aria-hidden="true"></span>
    <span class="inline-loader__label"></span>`;
  document.body.appendChild(el);
  _inlineEl    = el;
  _inlineLabel = el.querySelector(".inline-loader__label");
  return el;
}

function _refreshInlineLabel() {
  if (!_inlineLabel) return;
  let last = "";
  for (const v of _inlineTokens.values()) last = v;
  _inlineLabel.textContent = last || "";
}

/**
 * Paint a compact inline indicator (bottom-right pill, non-blocking).
 * @param {string} [label]
 * @returns {number} token — pass to hideInlineLoader() when done.
 */
export function showInlineLoader(label = "Working…") {
  if (_inlineLeave) { clearTimeout(_inlineLeave); _inlineLeave = null; }
  if (_inlineEl) _inlineEl.classList.remove("is-leaving");
  const id = _inlineNextId++;
  _inlineTokens.set(id, String(label || ""));
  _ensureInline();
  _refreshInlineLabel();
  return id;
}

export function hideInlineLoader(token) {
  if (token == null) return;
  _inlineTokens.delete(token);
  if (_inlineTokens.size > 0) { _refreshInlineLabel(); return; }
  if (!_inlineEl) return;
  _inlineEl.classList.add("is-leaving");
  const el = _inlineEl;
  _inlineLeave = setTimeout(() => {
    if (el && el.parentNode) el.parentNode.removeChild(el);
    if (_inlineEl === el) { _inlineEl = null; _inlineLabel = null; }
    _inlineLeave = null;
  }, 160);
}

/** Force-clear (used by page-teardown / router navigations). */
export function resetActionLoader() {
  // v12.13 (#B): also clear the inline indicator on route change.
  _inlineTokens.clear();
  if (_inlineLeave) { clearTimeout(_inlineLeave); _inlineLeave = null; }
  if (_inlineEl && _inlineEl.parentNode) {
    _inlineEl.parentNode.removeChild(_inlineEl);
  }
  _inlineEl = null;
  _inlineLabel = null;
  _tokens.clear();
  if (_leaveTimer) { clearTimeout(_leaveTimer); _leaveTimer = null; }
  if (_overlayEl && _overlayEl.parentNode) {
    _overlayEl.parentNode.removeChild(_overlayEl);
  }
  _overlayEl = null;
  _labelEl   = null;
}

