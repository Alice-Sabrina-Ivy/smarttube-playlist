FROM python:3.12-slim

# The licence label describes THIS IMAGE, not the repository. The project's own
# source is MIT, but the image bundles pyytlounge, which is GPL-3.0. The image is
# therefore a combined work carrying GPL-3.0 obligations, while the source on
# GitHub remains MIT. See THIRD_PARTY_NOTICES.md.
LABEL org.opencontainers.image.title="SmartTube Playlist" \
      org.opencontainers.image.description="LAN-only web service for queueing YouTube videos to SmartTube" \
      org.opencontainers.image.source="https://github.com/Alice-Sabrina-Ivy/smarttube-playlist" \
      org.opencontainers.image.documentation="https://github.com/Alice-Sabrina-Ivy/smarttube-playlist#readme" \
      org.opencontainers.image.licenses="GPL-3.0-or-later"

WORKDIR /app

# Build deps for cryptography (used by androidtvremote2 for TLS); removed after install.
# gosu is kept around at runtime for the entrypoint's privilege drop.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libffi-dev gosu \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y gcc libffi-dev \
    && apt-get autoremove -y

COPY app.py events.py lounge.py metadata.py playlist.py ratelimit.py ./
# Single source of version truth; /api/status serves it so a running container
# can say what it is.
COPY VERSION ./
COPY index.html ./
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Create the app user (UID 1000). We do NOT switch to it via USER —
# entrypoint.sh runs as root briefly to chown /data, then drops to app
# via gosu. This avoids the bind-mount permission trap.
RUN useradd -u 1000 -m app

ENV DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Liveness probe via /healthz. Portainer + Docker show a green/red dot
# based on this. Uses python's urllib (no curl/wget needed in the slim
# base image). 30s interval, 5s timeout, 3 retries before "unhealthy",
# 15s grace period at startup so initial pairing/lounge connect doesn't
# count against us.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
