# ============================================================================
# Dockerfile — Optional standalone container for the Mini App backend.
# ============================================================================
# You do NOT need this if you're deploying together with the existing bot on
# Render (start.sh handles it). Use this only if you want to run the Mini App
# on a separate service (e.g. a second Render web service, Fly.io, etc.).
#
# Build:  docker build -t doujinshi-universe -f backend/Dockerfile .
# Run:    docker run -p 8000:8000 --env-file .env doujinshi-universe
# ============================================================================

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install deps first — better Docker layer caching
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install -r backend/requirements.txt

# Copy the whole miniapp/ tree (backend + frontend static files)
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Non-root user for safety
RUN useradd --create-home --shell /bin/bash appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Render / Fly.io / etc set $PORT — fall back to 8000 locally
CMD ["sh", "-c", "uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --log-level ${MINIAPP_LOG_LEVEL:-info}"]
