FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="Linearr" \
      org.opencontainers.image.description="Custom Plex playlists across multiple TV shows (and movies) — round-robin or chronological with crossover alignment." \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/gillberg1111/linearr"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=5005 \
    DB_PATH=/data/rotator.db

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 5005

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5005/', timeout=3).status==200 else 1)"

CMD ["python", "app.py"]
