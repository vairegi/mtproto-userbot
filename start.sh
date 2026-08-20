#!/usr/bin/env bash
# =============================================================================
# start.sh — single entrypoint that replaces PM2.
#
# v12.31 topology (Render 512 MB free tier) — 3 resident processes:
#   1) Pre-flight: env + MongoDB + ONE-SHOT Telethon session check
#      (userbot.py runs BLOCKING here, then exits — no longer resident).
#   2) Background supervised: admin_bot.py + worker.py.
#      relay.py (legacy V1) was REMOVED in v12.31; worker.py routes every
#      job directly to relay_v2.
#   3) Foreground: uvicorn (Mini App backend). It is the process Render's
#      port scanner needs open, so it gets the foreground slot (was held
#      by userbot.py before, which added ~60-80 MB resident for no reason
#      — worker.py owns the real Telethon client via userbot.build_client).
# =============================================================================

set -uo pipefail

# ---------------------------------------------------------------------------
# 0. Environment
# ---------------------------------------------------------------------------
export PYTHONUNBUFFERED=1          # show logs live instead of in blocks
export PYTHONDONTWRITEBYTECODE=1   # no .pyc clutter on a read-only disk

cd "$(dirname "$0")" || exit 1
APP_DIR="$(pwd)"
export PYTHONPATH="${APP_DIR}:${PYTHONPATH:-}"

# Pick whichever Python is available.
PY="$(command -v python3 || command -v python)"
if [ -z "${PY}" ]; then
  echo "FATAL: no python3 interpreter found on PATH" >&2
  exit 1
fi

# Prefer app-local logs; fall back to /tmp if the filesystem is read-only.
LOG_DIR="${APP_DIR}/logs"
if ! mkdir -p "${LOG_DIR}" 2>/dev/null || [ ! -w "${LOG_DIR}" ]; then
  LOG_DIR="/tmp/logs"
  mkdir -p "${LOG_DIR}" 2>/dev/null || true
fi
export LOG_DIR

log() { echo "[start.sh $(date -u '+%Y-%m-%d %H:%M:%S')] $*"; }

log "============================================================"
log "MTProto relay bot starting (v12.31 3-process topology)"
log "  app dir : ${APP_DIR}"
log "  python  : ${PY} ($(${PY} --version 2>&1))"
log "  logs    : ${LOG_DIR}"
log "============================================================"

# ---------------------------------------------------------------------------
# 1. Pre-flight checks — fail loudly and early with a readable message
# ---------------------------------------------------------------------------
MISSING=""
for var in API_ID API_HASH BOT_TOKEN MONGO_URI STRING_SESSION; do
  case "${var}" in
    API_ID)         [ -n "${API_ID:-}${TELEGRAM_API_ID:-}" ]                 || MISSING="${MISSING} API_ID" ;;
    API_HASH)       [ -n "${API_HASH:-}${TELEGRAM_API_HASH:-}" ]             || MISSING="${MISSING} API_HASH" ;;
    BOT_TOKEN)      [ -n "${BOT_TOKEN:-}${ADMIN_BOT_TOKEN:-}" ]              || MISSING="${MISSING} BOT_TOKEN" ;;
    MONGO_URI)      [ -n "${MONGO_URI:-}${MONGODB_URI:-}" ]                  || MISSING="${MISSING} MONGO_URI" ;;
    STRING_SESSION) [ -n "${STRING_SESSION:-}${TELEGRAM_SESSION_STRING:-}" ] || MISSING="${MISSING} STRING_SESSION" ;;
  esac
done

if [ -n "${MISSING}" ]; then
  log "FATAL: missing required environment variable(s):${MISSING}"
  log ""
  log "Add them in your hosting platform's Secrets/Variables panel:"
  log "  Render              -> Environment -> Environment Variables"
  log "  Hugging Face Spaces -> Settings -> Variables and secrets"
  log ""
  log "Required: API_ID, API_HASH, BOT_TOKEN, MONGO_URI, STRING_SESSION,"
  log "          ADMIN_USER_ID, BOT2_USERNAME, DATABASE_CHANNEL_ID"
  exit 1
fi
log "env check: all required variables are present"

log "checking MongoDB connection..."
if ${PY} -c "
import sys
try:
    import db
    db.init_db()
    conn = db.connect()
    print('[start.sh] MongoDB OK - database:', conn.db.name)
    conn.close()
except Exception as e:
    print('[start.sh] MongoDB FAILED:', e, file=sys.stderr)
    sys.exit(1)
"; then
  log "MongoDB connection verified"
else
  log "FATAL: cannot reach MongoDB. Check that:"
  log "  1. MONGO_URI is copied correctly (starts with mongodb+srv://)"
  log "  2. <db_password> in the URI was replaced with your real password"
  log "  3. Special characters in the password are URL-encoded (@ -> %40)"
  log "  4. Atlas -> Network Access allows 0.0.0.0/0"
  exit 1
fi

# ---------------------------------------------------------------------------
# 1b. One-shot Telethon session check — userbot.py connects, verifies the
#     STRING_SESSION is still authorized, prints who we logged in as, then
#     EXITS. v12.31: this used to be the resident foreground process,
#     keeping a Telethon client alive for the whole container lifetime for
#     no reason (worker.py owns the real client via userbot.build_client).
#     Now it's a boot-time validator only — same failure signal, no RSS.
# ---------------------------------------------------------------------------
log "validating Telethon STRING_SESSION..."
${PY} -u userbot.py
USERBOT_CODE=$?
if [ ${USERBOT_CODE} -ne 0 ]; then
  log "FATAL: userbot.py session check failed (code=${USERBOT_CODE})"
  log "  check API_ID / API_HASH / STRING_SESSION"
  exit ${USERBOT_CODE}
fi
log "Telethon session verified"

# ---------------------------------------------------------------------------
# 2. Process supervision helpers
# ---------------------------------------------------------------------------
PIDS=""

# Forward shutdown signals to every child so the container stops cleanly.
shutdown() {
  log "shutdown signal received — stopping child processes"
  # Graceful phase — 12 seconds. Telegram closes an idle long-poll in <10s,
  # so this window lets admin_bot's polling connection actually release its
  # slot with the Telegram API before we hard-kill. Without this, the next
  # container instance racing to boot gets Conflict for ~30s.
  for pid in ${PIDS}; do
    kill -TERM "${pid}" 2>/dev/null || true
  done
  # Wait up to 12s for children to exit voluntarily.
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    alive=0
    for pid in ${PIDS}; do
      kill -0 "${pid}" 2>/dev/null && alive=$((alive+1))
    done
    [ "${alive}" -eq 0 ] && break
    sleep 1
  done
  # Anything still alive → SIGKILL.
  for pid in ${PIDS}; do
    kill -KILL "${pid}" 2>/dev/null || true
  done
  log "all children stopped"
  exit 0
}
trap shutdown SIGTERM SIGINT

supervise() {
  script="$1"
  label="$2"

  local MAX_CONSECUTIVE_CRASHES=50   
  local HEALTHY_UPTIME_SEC=60        
  local CLEAN_EXIT_DELAY=2           
  local FAST_CRASH_DELAY=15          
  local NORMAL_CRASH_DELAY=5         

  (
    consecutive_crashes=0
    total_starts=0
    total_clean_exits=0
    total_crashes=0

    while :; do
      start_time=$(date +%s)
      total_starts=$(( total_starts + 1 ))
      log "[${label}] starting (start #${total_starts})"
      ${PY} -u "${script}"
      code=$?
      end_time=$(date +%s)
      uptime=$(( end_time - start_time ))

      if [ ${code} -eq 0 ]; then
        total_clean_exits=$(( total_clean_exits + 1 ))
        consecutive_crashes=0
        # admin_bot must wait extra before restart so Telegram's polling
        # slot from the PREVIOUS process definitely releases. relay.py and
        # worker.py don't touch getUpdates so they can restart fast.
        if [ "${label}" = "admin_bot" ]; then
          delay=8
        else
          delay=${CLEAN_EXIT_DELAY}
        fi
        log "[${label}] clean exit (code=0) after ${uptime}s — cycle #${total_clean_exits} complete; restarting in ${delay}s"
        sleep ${delay}
        continue
      fi

      total_crashes=$(( total_crashes + 1 ))

      if [ ${uptime} -ge ${HEALTHY_UPTIME_SEC} ]; then
        consecutive_crashes=1
        log "[${label}] crashed (code=${code}) after ${uptime}s of healthy uptime — resetting crash streak"
      else
        consecutive_crashes=$(( consecutive_crashes + 1 ))
      fi

      if [ ${consecutive_crashes} -ge ${MAX_CONSECUTIVE_CRASHES} ]; then
        log "[${label}] ${MAX_CONSECUTIVE_CRASHES} back-to-back rapid crashes — giving up to avoid a hot restart loop"
        log "[${label}] totals: ${total_starts} starts, ${total_clean_exits} clean cycles, ${total_crashes} crashes"
        break
      fi

      if [ ${uptime} -lt 10 ]; then
        delay=${FAST_CRASH_DELAY}
      else
        delay=${NORMAL_CRASH_DELAY}
      fi
      # admin_bot crash → likely Conflict → force extra 15s so Telegram's
      # server-side polling slot from the crashed instance definitely
      # expires (empirically ~10-30s) before we reconnect.
      if [ "${label}" = "admin_bot" ] && [ "${code}" != "0" ]; then
        delay=$(( delay + 15 ))
      fi
      log "[${label}] crashed code=${code} after ${uptime}s — restarting in ${delay}s (consecutive rapid crashes: ${consecutive_crashes}/${MAX_CONSECUTIVE_CRASHES})"
      sleep ${delay}
    done

    log "[${label}] supervisor exiting after ${total_starts} total starts (${total_clean_exits} clean, ${total_crashes} crashed)"
  ) &
  pid=$!
  PIDS="${PIDS} ${pid}"
  log "[${label}] supervisor pid=${pid}"
}

# ---------------------------------------------------------------------------
# 3. Launch background processes & Mini App Web Server
# ---------------------------------------------------------------------------
log "------------------------------------------------------------"
log "launching background processes"
log "------------------------------------------------------------"

supervise "admin_bot.py" "admin_bot"
sleep 3                      # let the Admin Bot claim the Telegram polling slot

supervise "worker.py" "worker"
sleep 2

# ---------------------------------------------------------------------------
# 4. Foreground: Mini App backend (uvicorn) on ${PORT:-8000}
#    Must be FOREGROUND so Render's port scanner sees an open port and the
#    container has a PID 1 to hold it alive. v12.31: uvicorn moved here
#    from a supervised background slot — pre-v12.31 it was OOM-killed
#    before it could bind ("No open ports detected" in the Render log),
#    and the foreground slot was wasted on a userbot.py process that had
#    already finished its session check and was just parked.
# ---------------------------------------------------------------------------
MINIAPP_ROOT="${APP_DIR}/miniapp"

if [ ! -d "${MINIAPP_ROOT}" ]; then
  log "FATAL: ${MINIAPP_ROOT} missing — cannot start Mini App backend"
  shutdown
  exit 1
fi

log "------------------------------------------------------------"
log "starting Mini App backend (uvicorn) on port ${PORT:-8000} in the foreground"
log "------------------------------------------------------------"

cd "${MINIAPP_ROOT}"
export PYTHONPATH="$(dirname "${MINIAPP_ROOT}"):${MINIAPP_ROOT}/backend:${PYTHONPATH:-}"

# exec replaces this shell so signals go straight to uvicorn; the trap on
# SIGTERM/SIGINT installed above still fires for the supervised children
# before uvicorn exits the container.
exec uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --log-level "${MINIAPP_LOG_LEVEL:-info}"
