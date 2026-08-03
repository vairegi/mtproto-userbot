/*
  back-stack.js — Global "back" gesture stack

  Telegram Mini Apps have a native back button (top-left, provided by the
  WebApp SDK). Without a handler, tapping it closes the app entirely. We
  want it to:
    1. Close any open bottom-sheet first
    2. Only close the app once nothing else is dismissible

  Any component that opens a dismissible layer (sheet, modal, etc.) pushes
  a handler onto this stack. When the back button fires, we pop and invoke
  the top handler. If the stack is empty, we let Telegram do its default
  (close the app).

  This is deliberately tiny and framework-free. Components use it by
  importing { push } and calling the returned pop function on teardown.
*/

import { showBackButton } from "core/telegram.js";

const stack = [];
let teardownBackBtn = null;

function ensureButton() {
  if (teardownBackBtn) return;
  teardownBackBtn = showBackButton(() => {
    const top = stack.pop();
    if (top) {
      try { top(); } catch (_) { /* ignore */ }
    }
    if (stack.length === 0) {
      // No more dismissible layers — hide the back button so the next
      // press exits the app naturally.
      if (teardownBackBtn) { teardownBackBtn(); teardownBackBtn = null; }
    }
  });
}

export function push(handler) {
  stack.push(handler);
  ensureButton();
  // Return a pop function so the caller can dismiss itself programmatically
  // and remove its handler from the stack (e.g. when a sheet is closed by
  // tapping the backdrop, not by the back button).
  return () => {
    const idx = stack.indexOf(handler);
    if (idx >= 0) stack.splice(idx, 1);
    if (stack.length === 0 && teardownBackBtn) {
      teardownBackBtn(); teardownBackBtn = null;
    }
  };
}

export function depth() { return stack.length; }
