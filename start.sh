#!/usr/bin/env bash
# =============================================================================
# start.sh — single entrypoint that replaces PM2.
#
# WHY THIS EXISTS
# ---------------
# PM2 is a Node.js process manager. It expects a long-lived server with a
# writable home directory and the ability to install global npm packages, none
# of which is guaranteed on free serverless hosting (Hugging Face Spaces,
# Render, Railway, Fly.io). This script does the same essential job using only
# bash, which is available everywhere.
#
# WHAT IT DOES
# ------------
#   * admin_bot.py  -> background
#   * worker.py     -> background
#   * relay.py      -> background   (see IMPORTANT note below)
#   * userbot.py    -> FOREGROUND
#
# The foreground process matters: a container stays alive only as long as its
# main command is running. If every process were backgrounded the script would
# reach the end, exit 0, and the platform would shut the container down.
#
# CRASH vs CLEAN-EXIT ACCOUNTING (2026-07-29 fix)
# -----------------------------------------------
# The previous version of this script counted EVERY exit — including clean
# `code=0` cycle completions — toward a 50-restart ceiling. On Render, relay.py
# finishes its polling cycle in 10-30 seconds and exits cleanly; that is normal
# and healthy, not a crash. After ~50 clean cycles (about 15 minutes) the
# supervisor would give up and the bot would go silent.
#
# The rules are now:
#   * code == 0                 -> ALWAYS treated as a clean exit. Never
#                                  counted toward the crash limit. Restarted
#                                  immediately (short delay).
#   * code != 0                 -> counted as a crash.
#   * A process that ran for >= 60 seconds before a non-zero exit RESETS the
#     crash counter to 0. That way a genuine crash-loop (rapid failures) still
#     trips the safety limit, but a bot that has been healthy for a while
#     never uses up its restart budget.
#   * The safety ceiling is only for BACK-TO-BACK rapid crashes.
#
# USAGE
#   bash start.sh
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

# ---------------------------------------------------------------------------
# supervise <script> <label>
# ---------------------------------------------------------------------------
# Runs a script in a restart loop, in the background.
#
# Restart accounting (fixed 2026-07-29):
#   * A "crash" == the process exited with a NON-ZERO status.
#   * A "clean exit" (status 0) is treated as one completed cycle: restart
#     immediately, do NOT touch the crash counter, do NOT count toward the
#     50-restart safety ceiling.
#   * The crash counter resets to 0 the moment the process runs for
#     HEALTHY_UPTIME_SEC (60s) or longer, so a bot that has been healthy for
#     a while always has a fresh restart budget.
#   * The 50-crash ceiling only trips on RAPID BACK-TO-BACK crashes, which is
#     what the ceiling is actually there to protect against (a hot restart
#     loop that would burn CPU forever).
supervise() {
  script="$1"
  label="$2"

  # Tunables
  local MAX_CONSECUTIVE_CRASHES=50   # only rapid-fire crashes count
  local HEALTHY_UPTIME_SEC=60        # >= this uptime clears the crash streak
  local CLEAN_EXIT_DELAY=2           # short pause between healthy work cycles
  local FAST_CRASH_DELAY=15          # back off harder on instant crashes
  local NORMAL_CRASH_DELAY=5         # standard crash-restart delay

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

      # ---- CLEAN EXIT (code 0) --------------------------------------------
      # Always treat as a healthy cycle completion, regardless of uptime.
      # Do NOT count toward the crash ceiling. Reset the streak so any prior
      # crash counter is cleared.
      if [ ${code} -eq 0 ]; then
        total_clean_exits=$(( total_clean_exits + 1 ))
        consecutive_crashes=0
        log "[${label}] clean exit (code=0) after ${uptime}s — cycle #${total_clean_exits} complete; restarting in ${CLEAN_EXIT_DELAY}s"
        sleep ${CLEAN_EXIT_DELAY}
        continue
      fi

      # ---- CRASH (non-zero) ----------------------------------------------
      total_crashes=$(( total_crashes + 1 ))

      # A crash that came AFTER a long healthy uptime clears the streak: the
      # process was fine for a while, it just hit one bad event. Count this
      # crash as #1 of a fresh streak, not #N of the previous one.
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

      # Back off harder on instant crashes so we never spin the CPU.
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
# 3. Launch the three background processes
# ---------------------------------------------------------------------------
log "------------------------------------------------------------"
log "launching background processes"
log "------------------------------------------------------------"

supervise "admin_bot.py" "admin_bot"
sleep 3                      # let the Admin Bot claim the Telegram polling slot

supervise "worker.py" "worker"
sleep 2

supervise "relay.py" "relay"
sleep 1

# ---------------------------------------------------------------------------
# 4. Run userbot.py in the FOREGROUND
# ---------------------------------------------------------------------------
# This is what holds the container open. userbot.py in this project is a client
# factory (no main loop), so we run it once to validate the session, then keep
# a lightweight supervisor loop in the foreground.
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

# Foreground watchdog: keeps the container alive and reports health.
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
