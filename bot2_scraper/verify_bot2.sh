#!/usr/bin/env bash
set -u; cd "$(dirname "$0")"; fail=0
ok(){ printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad(){ printf '  \033[31mFAIL\033[0m  %s\n' "$*"; fail=$((fail+1)); }
echo "==> syntax"; while IFS= read -r -d '' f; do
  python3 -m py_compile "$f" 2>/dev/null || bad "compile: $f"
done < <(find . -name '*.py' -not -path '*/__pycache__/*' -print0)
[ $fail -eq 0 ] && ok "all python compiles"
echo "==> isolation"
[ -e admin_bot.py ] && bad 'admin_bot.py must not be in Bot 2' || ok 'no admin_bot.py'; for f in worker.py userbot.py relay_v2.py hf_scraper.py; do [ -e "$f" ] && ok "$f present" || bad "$f missing"; done
echo "==> requirements"
grep -qi '^telethon' requirements_bot2.txt && ok 'Telethon in Bot 2 requirements' || bad 'Telethon missing from Bot 2 requirements'
echo "==> start.sh"
grep -q 'worker.py' start.sh && ok 'start.sh spawns worker.py' || bad 'start.sh must spawn worker.py'
if [ $fail -eq 0 ]; then printf '\033[32mBot 2 verification PASSED\033[0m\n'; else printf '\033[31mBot 2 verification FAILED (%d)\033[0m\n' "$fail"; exit 1; fi
