FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first so the layer caches independently of source changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# SQLite and the poster cache live here; mount a volume over it.
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8737

# Run as non-root. /data must be writable by this uid, which the
# entrypoint chown handles for freshly-created bind mounts.
RUN useradd --system --uid 1001 --create-home pinnarr \
 && chown -R pinnarr:pinnarr /app /data
USER pinnarr

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8737/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8737"]
