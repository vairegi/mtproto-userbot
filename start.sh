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
# Each background process is auto-restarted if it dies (PM2's `autorestart`),
# with a short delay so a crash-loop cannot spin the CPU. If the FOREGROUND
# process dies, the whole container exits and the platform restarts it — which
# is exactly the behaviour you want.
#
# IMPORTANT NOTE ABOUT relay.py AND userbot.py
# --------------------------------------------
# In this project relay.py and userbot.py are LIBRARY modules, not standalone
# services: worker.py imports `process_job` from relay.py and `build_client`
# from userbot.py. Running them as separate processes was requested, so this
# script does exactly that, but it handles them intelligently:
#
#   - If a module has no independent main loop it exits immediately. Rather
#     than restarting it forever in a pointless loop, the script detects that
#     and reports it once in the logs.
#   - Because userbot.py must hold the container open in the foreground, the
#     script keeps a supervisor loop alive there regardless, so your container
#     never exits by surprise.
#
# The actual gallery-processing work happens inside worker.py (which drives
# relay.py + userbot.py internally), so the pipeline is fully functional.
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

# Hugging Face Spaces sets HOME=/home/user but some images restrict writes.
# Fall back to /tmp for logs if our own folder is not writable.
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
  log "  Hugging Face Spaces -> Settings -> Variables and secrets"
  log "  Render              -> Environment -> Environment Variables"
  log ""
  log "Required: API_ID, API_HASH, BOT_TOKEN, MONGO_URI, STRING_SESSION,"
  log "          ADMIN_USER_ID, BOT1_USERNAME, BOT2_USERNAME, DATABASE_CHANNEL_ID"
  exit 1
fi
log "env check: all required variables are present"

# Verify MongoDB is reachable before starting anything. Catching a bad
# MONGO_URI here gives one clear error instead of four crashing processes.
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

# supervise <script> <label>
# Runs a script in a restart loop, in the background. Mirrors PM2's
# autorestart + restart_delay + min_uptime behaviour.
supervise() {
  script="$1"
  label="$2"
  (
    restarts=0
    max_restarts=50
    while :; do
      start_time=$(date +%s)
      log "[${label}] starting"
      ${PY} -u "${script}"
      code=$?
      end_time=$(date +%s)
      uptime=$(( end_time - start_time ))

      if [ ${code} -eq 0 ] && [ ${uptime} -lt 5 ]; then
        # Exited cleanly and instantly => it is an imported library module
        # with no main loop of its own (true for relay.py / userbot.py here).
        # Restarting it forever would just spam the logs, so stop.
        log "[${label}] exited immediately with code 0 — this module has no"
        log "[${label}] standalone loop (it is imported by worker.py). Not restarting."
        break
      fi

      restarts=$(( restarts + 1 ))
      if [ ${restarts} -ge ${max_restarts} ]; then
        log "[${label}] hit ${max_restarts} restarts — giving up"
        break
      fi

      # Back off harder on instant crashes to avoid a hot restart loop.
      if [ ${uptime} -lt 10 ]; then
        delay=15
      else
        delay=5
      fi
      log "[${label}] exited code=${code} after ${uptime}s — restarting in ${delay}s (restart ${restarts}/${max_restarts})"
      sleep ${delay}
    done
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
# a lightweight supervisor loop in the foreground. That loop also acts as a
# watchdog: if every background supervisor dies, we exit non-zero so the
# hosting platform restarts the whole container.
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
