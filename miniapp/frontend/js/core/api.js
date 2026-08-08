/*
  api.js — fetch() wrapper that attaches Telegram initData for auth.

  Every backend call goes through here. Change auth headers, base URL, or
  error handling in ONE place.

  Usage:
    import { api } from "core/api.js";
    const rows = await api.get("/api/search", { q: "vanilla", page: 1 });
    const res  = await api.post("/api/queue", { url: "https://nhentai.net/g/123/" });
*/

import { getInitData } from "core/telegram.js";

const BASE = ""; // same-origin — backend serves the frontend

function qs(params) {
  if (!params) return "";
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    u.set(k, String(v));
  }
  const s = u.toString();
  return s ? "?" + s : "";
}

async function request(method, path, { params, body, headers } = {}) {
  const initData = getInitData();
  const finalHeaders = {
    "Content-Type": "application/json",
    "X-Telegram-Init-Data": initData,
    ...(headers || {}),
  };
  const res = await fetch(`${BASE}${path}${qs(params)}`, {
    method,
    headers: finalHeaders,
    body: body ? JSON.stringify(body) : undefined,
  });
  const ct = res.headers.get("content-type") || "";
  const payload = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const err = new Error((payload && payload.detail) || `HTTP ${res.status}`);
    err.status = res.status;
    err.payload = payload;
    // v12.1: expose Retry-After so callers (prefetch circuit breaker in
    // plugins/detail-sheet.js) can honour upstream backoff without
    // re-fetching or racing another 503.
    const ra = res.headers.get("Retry-After");
    if (ra != null && ra !== "") {
      const n = Number(ra);
      err.retry_after = Number.isFinite(n) && n > 0 ? n : 60;
    }
    throw err;
  }
  return payload;
}

export const api = {
  get:  (path, params)       => request("GET",    path, { params }),
  post: (path, body, params) => request("POST",   path, { body, params }),
  put:  (path, body, params) => request("PUT",    path, { body, params }),
  del:  (path, params)       => request("DELETE", path, { params }),
};
