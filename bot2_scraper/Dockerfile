FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo zlib1g curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY requirements_bot2.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
RUN chmod +x /app/start.sh
EXPOSE 8000
CMD ["bash", "start.sh"]
