#!/usr/bin/env bash
# =============================================================================
# start.sh — single entrypoint (v12.33 3-process topology, 512MB-safe).
#   1) Pre-flight: env + MongoDB + ONE-SHOT Telethon session check
#      (userbot.py runs blocking here, then exits — not resident).
#      v12.33: only STRING_SESSION (slot 1) is checked here. Slot 2's
#      STRING_SESSION_2 is validated later by UserbotPool.start() inside
#      worker.py; a missing/blank STRING_SESSION_2 silently falls back
#      to a 1-slot pool (byte-equivalent to v12.32 for ordering).
#   2) Background supervised: admin_bot.py + worker.py.
#      relay.py (legacy V1) removed in v12.31; worker.py owns the
#      v12.33 multi-userbot pool.
#   3) Foreground: uvicorn (Mini App backend) — the port Render scans.
# =============================================================================

set -uo pipefail
export PYTHONUNBUFFERED=1
export PYTHONDONTWRITEBYTECODE=1

cd "$(dirname "$0")" || exit 1
APP_DIR="$(pwd)"
export PYTHONPATH="${APP_DIR}:${PYTHONPATH:-}"

PY="$(command -v python3 || command -v python)"
if [ -z "${PY}" ]; then
  echo "FATAL: no python3 interpreter found on PATH" >&2
  exit 1
fi

LOG_DIR="${APP_DIR}/logs"
if ! mkdir -p "${LOG_DIR}" 2>/dev/null || [ ! -w "${LOG_DIR}" ]; then
  LOG_DIR="/tmp/logs"; mkdir -p "${LOG_DIR}" 2>/dev/null || true
fi
export LOG_DIR

log() { echo "[start.sh $(date -u '+%Y-%m-%d %H:%M:%S')] $*"; }

log "============================================================"
log "MTProto relay bot starting (v12.33 3-process topology + userbot pool)"
log "  app dir : ${APP_DIR}"
log "  python  : ${PY} ($(${PY} --version 2>&1))"
log "  logs    : ${LOG_DIR}"
log "============================================================"

# --- 1. Pre-flight env check ----------------------------------------------
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
  log "FATAL: cannot reach MongoDB."
  exit 1
fi

# --- 1b. One-shot Telethon session check (then exits; not resident) -------
log "validating Telethon STRING_SESSION..."
${PY} -u userbot.py
USERBOT_CODE=$?
if [ ${USERBOT_CODE} -ne 0 ]; then
  log "FATAL: userbot.py session check failed (code=${USERBOT_CODE})"
  exit ${USERBOT_CODE}
fi
log "Telethon session verified"

# --- 2. Supervision helpers ------------------------------------------------
PIDS=""

shutdown() {
  log "shutdown signal received — stopping child processes"
  for pid in ${PIDS}; do kill -TERM "${pid}" 2>/dev/null || true; done
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
    alive=0
    for pid in ${PIDS}; do kill -0 "${pid}" 2>/dev/null && alive=$((alive+1)); done
    [ "${alive}" -eq 0 ] && break
    sleep 1
  done
  for pid in ${PIDS}; do kill -KILL "${pid}" 2>/dev/null || true; done
  log "all children stopped"
  exit 0
}
trap shutdown SIGTERM SIGINT

supervise() {
  script="$1"; label="$2"
  local MAX_CONSECUTIVE_CRASHES=50 HEALTHY_UPTIME_SEC=60
  local CLEAN_EXIT_DELAY=2 FAST_CRASH_DELAY=15 NORMAL_CRASH_DELAY=5
  (
    consecutive_crashes=0; total_starts=0; total_clean_exits=0; total_crashes=0
    while :; do
      start_time=$(date +%s); total_starts=$(( total_starts + 1 ))
      log "[${label}] starting (start #${total_starts})"
      ${PY} -u "${script}"
      code=$?; end_time=$(date +%s); uptime=$(( end_time - start_time ))
      if [ ${code} -eq 0 ]; then
        total_clean_exits=$(( total_clean_exits + 1 )); consecutive_crashes=0
        if [ "${label}" = "admin_bot" ]; then delay=8; else delay=${CLEAN_EXIT_DELAY}; fi
        log "[${label}] clean exit (code=0) after ${uptime}s — cycle #${total_clean_exits}; restarting in ${delay}s"
        sleep ${delay}; continue
      fi
      total_crashes=$(( total_crashes + 1 ))
      if [ ${uptime} -ge ${HEALTHY_UPTIME_SEC} ]; then consecutive_crashes=1; else consecutive_crashes=$(( consecutive_crashes + 1 )); fi
      if [ ${consecutive_crashes} -ge ${MAX_CONSECUTIVE_CRASHES} ]; then
        log "[${label}] ${MAX_CONSECUTIVE_CRASHES} rapid crashes — supervisor giving up"
        break
      fi
      if [ ${uptime} -lt 10 ]; then delay=${FAST_CRASH_DELAY}; else delay=${NORMAL_CRASH_DELAY}; fi
      if [ "${label}" = "admin_bot" ]; then delay=$(( delay + 15 )); fi
      log "[${label}] crashed code=${code} after ${uptime}s — restarting in ${delay}s (streak ${consecutive_crashes}/${MAX_CONSECUTIVE_CRASHES})"
      sleep ${delay}
    done
    log "[${label}] supervisor exiting after ${total_starts} starts"
  ) &
  pid=$!
  PIDS="${PIDS} ${pid}"
  log "[${label}] supervisor pid=${pid}"
}

# --- 3. Launch background supervised processes -----------------------------
log "------------------------------------------------------------"
log "launching background supervised processes"
log "------------------------------------------------------------"

supervise "admin_bot.py" "admin_bot"
sleep 3                      # let the Admin Bot claim the Telegram polling slot

supervise "worker.py" "worker"
sleep 2

# --- 4. Foreground: Mini App backend (uvicorn) on ${PORT:-8000} ------------
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

exec uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --log-level "${MINIAPP_LOG_LEVEL:-info}"
