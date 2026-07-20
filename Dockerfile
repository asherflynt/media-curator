FROM python:3.12-slim

LABEL org.opencontainers.image.title="media-curator" \
      org.opencontainers.image.description="Radarr/Sonarr library curator: reclaim disk by demoting over-quality files via Radarr's native replace path. Never re-encodes." \
      org.opencontainers.image.source="https://github.com/asherflynt/media-curator" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

ENV PYTHONUNBUFFERED=1 \
    MC_DB=/data/media-curator.db

EXPOSE 8420

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8420"]
