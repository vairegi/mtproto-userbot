#!/usr/bin/env bash
# ============================================================================
# verify.sh — Sanity check that a freshly-extracted checkpoint is intact.
# ============================================================================
# Run after unzipping a checkpoint. Confirms:
#   * every required file exists
#   * key files are non-empty
#   * python syntax is valid on every .py file
#   * javascript files have balanced braces (very rough)
#
# Exit code 0 iff everything looks good.
# ============================================================================
set -u
cd "$(dirname "$0")"

fail=0
say() { printf '  %s\n' "$*"; }
ok()  { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad() { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }

echo "==> Required files"
REQUIRED=(
  # v12.8 hygiene: README.md / CHANGELOG.md / docs/ removed from the
  # production tree — operator docs, never imported at runtime.
  start_patch.sh
  frontend/index.html
  frontend/css/theme.css frontend/css/base.css frontend/css/components.css
  frontend/js/core/app.js frontend/js/core/registry.js
  frontend/js/core/components.js frontend/js/core/api.js
  frontend/js/core/telegram.js frontend/js/core/state.js
  frontend/js/core/back-stack.js frontend/js/core/prefs.js
  frontend/js/pages/search.js frontend/js/pages/bookmarks.js
  frontend/js/pages/queue.js frontend/js/pages/profile.js
  frontend/js/pages/settings.js frontend/js/pages/admin.js
  frontend/js/plugins/card-actions.js
  frontend/js/plugins/search-operators.js
  frontend/js/plugins/preview-modal.js
  frontend/js/components/card.js frontend/js/components/chip.js
  frontend/js/components/sheet.js frontend/js/components/toast.js
  frontend/js/components/skeleton.js
  backend/main.py backend/requirements.txt
  backend/app/config.py backend/app/auth.py backend/app/db.py
  backend/app/ratelimit.py
  backend/app/routes/__init__.py backend/app/routes/profile.py
  backend/app/routes/search.py backend/app/routes/gallery.py
  backend/app/routes/queue.py backend/app/routes/bookmarks.py
  backend/app/routes/admin.py backend/app/routes/stats.py
  backend/app/routes/random.py
  backend/app/services/scraper_bridge.py
  backend/app/services/queue_bridge.py
  backend/tests/smoke_test.py
  integration/admin_bot_patch.py
)
for f in "${REQUIRED[@]}"; do
  if [ ! -s "$f" ]; then bad "$f missing or empty"; else ok "$f"; fi
done

echo
echo "==> Python syntax"
if command -v python3 >/dev/null; then
  while IFS= read -r py; do
    if python3 -m py_compile "$py" 2>/dev/null; then ok "$py"
    else bad "$py has syntax error"; fi
  done < <(find backend integration -name '*.py' -type f)
else
  say "(python3 not available — skipping)"
fi

echo
echo "==> Rough JS balance check"
while IFS= read -r js; do
  o=$(tr -cd '{' < "$js" | wc -c)
  c=$(tr -cd '}' < "$js" | wc -c)
  if [ "$o" = "$c" ]; then ok "$js ($o pairs)"; else bad "$js unbalanced braces $o/$c"; fi
done < <(find frontend/js -name '*.js' -type f)

echo
if [ "$fail" -eq 0 ]; then
  printf '\033[32m✓ Verification passed.\033[0m\n'
  exit 0
else
  printf '\033[31m✗ %d failure(s).\033[0m\n' "$fail"
  exit 1
fi
