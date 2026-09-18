# Multi-stage build for NAS FeedOps Sentinel
# Stage 1: build wheels for all deps
FROM python:3.13-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.txt

# Stage 2: runtime image
FROM python:3.13-slim AS runtime

# Non-root user
RUN groupadd -r sentinel && useradd -r -g sentinel -d /app -s /sbin/nologin sentinel

WORKDIR /app

# Install pre-built wheels
COPY --from=builder /wheels /wheels
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt \
    && rm -rf /wheels

# Application code
COPY src/ ./src/
COPY prompts/ ./prompts/
COPY dashboard/ ./dashboard/

# Writable volume for ingest folders
RUN mkdir -p /app/data/raw/swim_live /app/data/processed /app/data/failed \
    && chown -R sentinel:sentinel /app

USER sentinel

# Streamlit dashboard on 8501, Prometheus metrics on 9108
EXPOSE 8501 9108

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO

# Default: keep container alive watching the live SWIM folder, metrics exposed.
# NOTE: this main.py has no standalone "swim-folder" mode or --metrics flag (it
# starts the metrics server unconditionally) -- the live-folder watch here lives
# under --mode replay; --watch is what makes it long-running (fs-event driven +
# periodic reconciliation scan) instead of a single scan-then-exit pass. The
# docker-compose.yml services in this repo mostly override this CMD anyway
# (temporal-worker runs src.temporal_worker, which is the durable long-running
# ingest path); this is the fallback for running the image standalone.
CMD ["python", "-m", "src.main", "--mode", "replay", "--watch"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9108/health').read()" || exit 1