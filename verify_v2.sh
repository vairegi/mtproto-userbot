#!/usr/bin/env bash
# ============================================================================
# verify_v2.sh — Full V2 pre-deploy sanity check (root of the mtproto-userbot
# repository). Run this before `git push` / redeploy.
#
# It checks THREE things:
#   1) Every V2 file that must exist is present and non-empty.
#   2) Every root-level Python file passes `python3 -m py_compile`.
#   3) The mini-app's own verify.sh passes (delegates to miniapp/verify.sh).
#   4) (v12.32: tests_v2_smoke.py removed from repo)
#   5) (v12.33: multi-userbot pool — userbot_pool.py added, worker.py &
#      relay_v2.py refactored, /checkram in admin_bot.py; see GUIDE.md.)
#
# Exit code 0 iff every step passes.
# ============================================================================
set -u
cd "$(dirname "$0")"

fail=0
say() { printf '  %s\n' "$*"; }
ok()  { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
hr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

hr "==> 1. V2 required files"
V2_REQUIRED=(
  # v12.8 hygiene: docs/ removed from the production tree (they were
  # operator-facing reading, never imported by any running code — only
  # mentioned in comments). References in config.py / worker.py /
  # relay_v2.py / gallery_state.py comments are historical pointers
  # and do not affect runtime.
  # Root modules (V2)
  gallery_state.py
  cover_poster.py
  bot2_client.py
  relay_v2.py
  # Migration + tests
  scripts/migrate_v2_recover_stuck.py
  # Untouched files that V2 depends on
  admin_bot.py config.py db.py worker.py hf_scraper.py
  queue_service.py start.sh
  # Miniapp V2 edits (must not be blank)
  miniapp/backend/app/routes/queue.py
  miniapp/backend/app/routes/gallery.py
  miniapp/backend/app/services/queue_bridge.py
  miniapp/frontend/js/plugins/card-actions.js
  miniapp/frontend/js/pages/search.js
  miniapp/frontend/js/pages/bookmarks.js
  miniapp/frontend/js/pages/queue.js
)
for f in "${V2_REQUIRED[@]}"; do
  if [ ! -s "$f" ]; then bad "$f missing or empty"
  else ok "$f"; fi
done

hr "==> 2. Root Python syntax (py_compile)"
if command -v python3 >/dev/null; then
  # Compile every .py at the repo root + scripts/ (miniapp is done by miniapp/verify.sh).
  while IFS= read -r py; do
    if python3 -m py_compile "$py" 2>/dev/null; then ok "$py"
    else bad "$py has syntax error"; fi
  done < <(find . -maxdepth 2 -type f -name '*.py' \
                    -not -path './miniapp/*' \
                    -not -path './__pycache__/*')
else
  say "(python3 not available — skipping)"
fi

hr "==> 3. V2 grep-signals (regression tripwires)"
signal_test() {
  local label="$1" file="$2" pattern="$3"
  if grep -q -- "$pattern" "$file" 2>/dev/null; then ok "$label"
  else bad "$label — pattern not found in $file"; fi
}
signal_test "worker.py routes to relay_v2" worker.py "_relay_v2.process_job"
signal_test "worker.py has SELF_COVER_POST_ENABLED router" worker.py "SELF_COVER_POST_ENABLED"
signal_test "db.py has galleries collection" db.py "def galleries("
signal_test "db.py galleries indexes" db.py "idx_galleries_status"
signal_test "gallery_state has STATUS_FAILED_RECOVERED" gallery_state.py "STATUS_FAILED_RECOVERED"
signal_test "relay_v2 dedup + purge on Bot 2 error" relay_v2.py "purge=True"
signal_test "config.py BOT1 deprecation warning" config.py "_emit_bot1_deprecation_warning"
signal_test "admin_bot /search redirects to mini-app" admin_bot.py "Search has moved to the Mini App"
signal_test "miniapp queue.py has dedup_peek gate" miniapp/backend/app/routes/queue.py "dedup_peek"
signal_test "miniapp queue_bridge has gallery_status" miniapp/backend/app/services/queue_bridge.py "def gallery_status"
signal_test "miniapp gallery route has /status endpoint" miniapp/backend/app/routes/gallery.py "/status"
signal_test "card-actions.js has dynamic label" miniapp/frontend/js/plugins/card-actions.js "isCompleted(gallery)"

hr "==> 4. Miniapp inner verify.sh (delegated)"
if [ -x miniapp/verify.sh ] || [ -r miniapp/verify.sh ]; then
  if bash miniapp/verify.sh > /tmp/miniapp_verify.log 2>&1; then
    ok "miniapp/verify.sh passed"
  else
    bad "miniapp/verify.sh reported failures (see /tmp/miniapp_verify.log)"
    tail -20 /tmp/miniapp_verify.log | sed 's/^/    /'
  fi
else
  bad "miniapp/verify.sh not found"
fi


echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32m✓ V2 verification passed. Safe to deploy.\033[0m\n'
  exit 0
else
  printf '\033[31m✗ V2 verification found %d issues.\033[0m\n' "$fail"
  exit 1
fi
