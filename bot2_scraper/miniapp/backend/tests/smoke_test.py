"""
smoke_test.py — Minimal end-to-end smoke test.

Runs against a live deployment (or `uvicorn backend.main:app` locally) and
exercises the public endpoints WITHOUT a real Telegram initData. Requires
the dev escape hatch: BOT_TOKEN empty + ADMIN_USER_ID set → server
synthesises an admin user, so admin routes work too.

Usage:
    export MINIAPP_URL=http://localhost:8000
    python -m backend.tests.smoke_test

Prints PASS/FAIL per endpoint. Exit code 0 iff all pass.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import httpx

BASE = os.environ.get("MINIAPP_URL", "http://localhost:8000")


def _call(method: str, path: str, **kw) -> tuple[int, Any]:
    r = httpx.request(method, BASE + path, timeout=15, **kw)
    try: data = r.json()
    except Exception: data = r.text
    return r.status_code, data


def check(label: str, ok: bool, detail: str = "") -> bool:
    mark = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
    print(f"  {mark}  {label}" + (f"  ({detail})" if detail else ""))
    return ok


def main() -> int:
    print(f"Smoke test against {BASE}")
    ok_all = True

    code, body = _call("GET", "/healthz")
    ok_all &= check("GET /healthz", code == 200 and body.get("ok"),
                    f"status={code}")

    code, body = _call("GET", "/")
    ok_all &= check("GET / (index.html)",
                    code == 200 and "<html" in (body if isinstance(body, str) else ""),
                    f"status={code}")

    code, body = _call("GET", "/api/profile/me")
    ok_all &= check("GET /api/profile/me",
                    code == 200 and body.get("user_id"),
                    f"status={code}")

    code, body = _call("GET", "/api/search", params={"q": "", "page": 1, "per_page": 5})
    ok_all &= check("GET /api/search",
                    code == 200 and isinstance(body.get("items"), list),
                    f"status={code}, items={len(body.get('items', []))}")

    code, body = _call("GET", "/api/queue/status")
    ok_all &= check("GET /api/queue/status",
                    code in (200, 503),
                    f"status={code}")

    code, body = _call("GET", "/api/admin/visibility")
    ok_all &= check("GET /api/admin/visibility (admin gate)",
                    code == 200 and "public_mode" in body,
                    f"status={code}")

    print()
    print("Overall:", "\033[32mALL PASS\033[0m" if ok_all else "\033[31mSOME FAILED\033[0m")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
