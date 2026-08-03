#!/usr/bin/env bash
# ============================================================================
# start_patch.sh — Snippet to paste into your existing start.sh
# ============================================================================
# This is NOT a standalone script. It is the block that REPLACES the old
# "python3 -m http.server $PORT &" line in your existing start.sh.
#
# Paste this block at the top of start.sh, right after the pre-flight env
# checks and the MongoDB ping. Then remove the old dummy HTTP server line.
# ============================================================================

# --- Mini App: serves the frontend AND passes Render's port scan ---------
# The Mini App backend binds $PORT so Render's health-checker sees a live
# HTTP server. It also serves index.html at "/" for Telegram's WebView.
#
# If uvicorn crashes (e.g. missing pymongo), the outer start.sh restart
# budget catches it via the same code=0/code!=0 rule that guards the bot.

MINIAPP_ROOT="$(cd "$(dirname "$0")" && pwd)/miniapp"

if [ -d "$MINIAPP_ROOT" ]; then
    echo "[start.sh] Booting Mini App backend on port $PORT"
    (
        cd "$MINIAPP_ROOT"
        # Ensure Mini App can import parent modules (hf_scraper, db, queue_service)
        export PYTHONPATH="$(dirname "$MINIAPP_ROOT"):$MINIAPP_ROOT/backend:${PYTHONPATH:-}"
        exec uvicorn backend.main:app \
            --host 0.0.0.0 \
            --port "$PORT" \
            --log-level "${MINIAPP_LOG_LEVEL:-info}"
    ) &
    MINIAPP_PID=$!
    echo "[start.sh] Mini App PID=$MINIAPP_PID"
else
    echo "[start.sh] WARN: $MINIAPP_ROOT missing, falling back to dummy HTTP server"
    python3 -m http.server "$PORT" &
fi

# --- End of Mini App block ------------------------------------------------
# Everything below is your existing start.sh: admin_bot.py, worker.py, etc.
