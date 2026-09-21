# Multi-stage build for NAS FeedOps Sentinel
# Stage 1: build wheels for all deps
FROM python:3.12-slim AS builder
# Pinned to 3.12, not 3.13: requirements.lock hash-locks crewai==0.95.0
# (requirements.txt deliberately constrains crewai<1.0.0 -- 1.0 was a
# breaking API change from the version this app's Crew()/Agent()/Task()
# code was built and verified against), and that exact version's own
# metadata requires Python <3.13. The lock file was resolved as a coherent
# set under this Python range; building on 3.13 makes pip refuse to
# install crewai at all (ERROR: No matching distribution found).

WORKDIR /build
# Install from requirements.lock (exact pinned versions + sha256 hashes,
# generated via `uv pip compile --generate-hashes`), not requirements.txt.
# requirements.txt uses loose ranges (crewai<1.0.0,>=0.80.0, openai>=1.40.0,
# etc.), which forces pip's resolver to backtrack across dozens of candidate
# versions to find a mutually-compatible set -- e.g. probing 15+ versions of
# openai and 40+ of instructor on every clean build, downloading metadata
# (and sometimes full wheels) for each one it rejects. requirements.lock
# already has the resolved, tested answer, so there is nothing left to solve:
# pip installs the exact listed version of everything with hash verification,
# no backtracking, dramatically less bandwidth, and a reproducible build.
COPY requirements.lock .
RUN pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements.lock

# Stage 2: runtime image
FROM python:3.12-slim AS runtime

# Non-root user
RUN groupadd -r sentinel && useradd -r -g sentinel -d /app -s /sbin/nologin sentinel

WORKDIR /app

# Install pre-built wheels
COPY --from=builder /wheels /wheels
COPY requirements.lock .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.lock \
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