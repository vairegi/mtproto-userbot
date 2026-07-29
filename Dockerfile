# =============================================================================
# Dockerfile — Hugging Face Spaces (Docker SDK) entrypoint for the relay bot.
#
# Hugging Face Spaces builds this file automatically when the Space's SDK is
# set to "docker". The important constraints on that platform are:
#
#   * The container MUST run as a non-root user (UID 1000) named "user".
#   * The main process must stay in the foreground; when it exits, the Space
#     is stopped.
#   * The filesystem is largely read-only except for /home/user, /tmp, and
#     (for paid tiers) any Persistent Storage that has been enabled.
#
# All three constraints are handled here.
# =============================================================================

FROM python:3.11-slim

# ---------------------------------------------------------------------------
# System packages
# ---------------------------------------------------------------------------
# ca-certificates: needed so pymongo can validate the Atlas TLS certificate.
# tini: PID-1 init so signals (SIGTERM from Spaces "restart" button) reach our
#       child processes cleanly and zombie processes get reaped.
# curl: harmless, useful for the container's HEALTHCHECK.
# ---------------------------------------------------------------------------
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        curl && \
    rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# Non-root user (required by Hugging Face Spaces)
# ---------------------------------------------------------------------------
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /home/user/app

# ---------------------------------------------------------------------------
# Python dependencies (cached layer — only rebuilt when requirements change)
# ---------------------------------------------------------------------------
COPY --chown=user:user requirements.txt ./
RUN pip install --user --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Application code
# ---------------------------------------------------------------------------
COPY --chown=user:user . .

# start.sh is our entrypoint; make sure it's executable inside the image.
RUN chmod +x start.sh

# ---------------------------------------------------------------------------
# Runtime configuration
# ---------------------------------------------------------------------------
# Log directory: prefer the mounted app folder, fall back to /tmp if the
# platform mounts the app directory read-only (start.sh does the fallback).
ENV LOG_DIR=/home/user/app/logs

# Health check: verify MongoDB is still reachable. Spaces doesn't actually
# use this, but it makes `docker ps` on your own machine much friendlier.
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
    CMD python -c "import db; db.ping() or (_ for _ in ()).throw(SystemExit(1))" || exit 1

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
# `tini` reaps zombies and forwards signals cleanly to bash, which in turn
# forwards them to the four Python processes.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["bash", "start.sh"]
