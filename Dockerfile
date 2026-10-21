# Aircraft Noise recorder and web server.
# Runs plain HTTP on 8787 inside the container; put TLS on the proxy or tunnel in front.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PN_DB_PATH=/app/data/plane_noise.db \
    COMMUNITY_DATA_PATH=/app/data/monitors.json \
    TLS_DISABLED=true \
    FORCE_HTTPS=false \
    SESSION_SECURE=auto

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gosu && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN groupadd -r planenoise && useradd -r -g planenoise -d /app planenoise \
    && mkdir -p /app/data \
    && chown -R planenoise:planenoise /app \
    && chmod +x /app/deploy/docker-entrypoint.sh
# The entrypoint starts as root only to make the mounted data folder writable,
# then drops to the planenoise user.
ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]

VOLUME ["/app/data"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/health', timeout=8).status == 200 else 1)"

CMD ["python", "-m", "gunicorn", "-c", "deploy/gunicorn.conf.py", "proxy_server:app"]
