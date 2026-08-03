#!/usr/bin/env bash
# =============================================================================
# start.sh — single entrypoint that replaces PM2.
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
log "MTProto relay bot starting"
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
  log "          ADMIN_USER_ID, BOT1_USERNAME, BOT2_USERNAME, DATABASE_CHANNEL_ID"
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
# 2. Process supervision helpers
# ---------------------------------------------------------------------------
PIDS=""

# Forward shutdown signals to every child so the container stops cleanly.
shutdown() {
  log "shutdown signal received — stopping child processes"
  for pid in ${PIDS}; do
    kill -TERM "${pid}" 2>/dev/null || true
  done
  sleep 2
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
        log "[${label}] clean exit (code=0) after ${uptime}s — cycle #${total_clean_exits} complete; restarting in ${CLEAN_EXIT_DELAY}s"
        sleep ${CLEAN_EXIT_DELAY}
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

# --- Mini App: serves the frontend AND passes Render's port scan ---
MINIAPP_ROOT="$(cd "$(dirname "$0")" && pwd)/miniapp"

if [ -d "$MINIAPP_ROOT" ]; then
    log "[start.sh] Booting Mini App backend on port ${PORT:-8000}"
    (
        cd "$MINIAPP_ROOT"
        export PYTHONPATH="$(dirname "$MINIAPP_ROOT"):$MINIAPP_ROOT/backend:${PYTHONPATH:-}"
        exec uvicorn backend.main:app \
            --host 0.0.0.0 \
            --port "${PORT:-8000}" \
            --log-level "${MINIAPP_LOG_LEVEL:-info}"
    ) &
    MINIAPP_PID=$!
    PIDS="${PIDS} ${MINIAPP_PID}"
    log "[start.sh] Mini App PID=${MINIAPP_PID} on port ${PORT:-8000}"
else
    log "[start.sh] WARN: $MINIAPP_ROOT missing, skipping Mini App startup"
fi
# --- End of Mini App block ---

supervise "admin_bot.py" "admin_bot"
sleep 3                      # let the Admin Bot claim the Telegram polling slot

supervise "worker.py" "worker"
sleep 2

supervise "relay.py" "relay"
sleep 1

# ---------------------------------------------------------------------------
# 4. Run userbot.py in the FOREGROUND
# ---------------------------------------------------------------------------
log "------------------------------------------------------------"
log "starting userbot.py in the foreground"
log "------------------------------------------------------------"

${PY} -u userbot.py
USERBOT_CODE=$?
log "userbot.py returned code=${USERBOT_CODE}"

if [ ${USERBOT_CODE} -ne 0 ]; then
  log "userbot.py failed — check API_ID / API_HASH / STRING_SESSION"
  shutdown
  exit ${USERBOT_CODE}
fi

log "userbot.py validated OK; entering foreground watchdog loop"
log "the bot is now running — background processes are supervised"

while :; do
  sleep 60
  alive=0
  for pid in ${PIDS}; do
    if kill -0 "${pid}" 2>/dev/null; then
      alive=$(( alive + 1 ))
    fi
  done
  if [ ${alive} -eq 0 ]; then
    log "FATAL: every background supervisor has exited — exiting so the"
    log "platform restarts the container"
    exit 1
  fi
done
