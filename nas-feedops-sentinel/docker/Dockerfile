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

# Writable volume for SQLite + ingest folders
RUN mkdir -p /app/data/raw/swim_live /app/data/processed /app/data/failed \
    && chown -R sentinel:sentinel /app

USER sentinel

# Streamlit dashboard on 8501, Prometheus metrics on 9108
EXPOSE 8501 9108

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_LEVEL=INFO

# Default: keep container alive running the swim-folder watcher with metrics exposed
CMD ["python", "-m", "src.main", "--mode", "swim-folder", "--metrics"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:9108/health').read()" || exit 1