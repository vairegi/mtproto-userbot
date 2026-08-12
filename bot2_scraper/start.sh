#!/usr/bin/env bash
# Bot 2 (scraper + worker) — runs worker.py ONLY. Deploy as a Render
# Background Worker (no port, no health-check URL needed).
set -eu
echo "[bot2_scraper 12.19] boot"
exec python3 worker.py
