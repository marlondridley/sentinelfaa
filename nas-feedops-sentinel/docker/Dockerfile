# Multi-stage build for NAS FeedOps Sentinel
# Stage 1: build wheels for all deps
FROM python:3.12-slim AS builder
# Python 3.12 is what requirements.lock is resolved for (see the compile
# command in its header). Moving to 3.13 is a matter of recompiling
# the locks and running the suite.

WORKDIR /build
# Install from requirements.lock (exact pinned versions + sha256 hashes,
# generated via `uv pip compile --generate-hashes`), not requirements.txt.
# requirements.txt uses loose ranges (openai>=1.40.0, instructor, etc.), which forces pip's resolver to backtrack across dozens of candidate
# versions to find a mutually-compatible set -- e.g. probing 15+ versions of
# openai and 40+ of instructor on every clean build, downloading metadata
# (and sometimes full wheels) for each one it rejects. requirements.lock
# already has the resolved, tested answer, so there is nothing left to solve:
# pip installs the exact listed version of everything with hash verification,
# no backtracking, dramatically less bandwidth, and a reproducible build.
COPY requirements.lock requirements-dev.lock ./
RUN pip wheel --no-cache-dir --wheel-dir=/wheels -r requirements-dev.lock

# Stage 2: runtime image
FROM python:3.12-slim AS runtime

# Security fixes Debian has published since the base image was built. python:3.12-slim lags the security archive
# (on 2026-10-01 it still carried openssl 3.5.7-1~deb13u2 while u3 fixed two HIGH CVEs), and the image-scan job
# fails the build on any fixable HIGH or CRITICAL finding. Upgrading installed packages only: nothing is added.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Non-root user, uid/gid 999 pinned: the Vault Agent and spiffe-helper
# sidecars run as this same uid and share memory-only volumes with the
# service, owner-only (docker-compose.yml, x-secret-tmpfs).
RUN groupadd -r -g 999 sentinel && useradd -r -u 999 -g sentinel -d /app -s /sbin/nologin sentinel

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
# Governance policies and model objectives (src/governance.py refuses every
# model call and tool call when its policy file is missing).
COPY policies/ ./policies/

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

# ── Test image: runtime + pytest/ruff + tests. Built by CI, never shipped. ──
FROM runtime AS test
USER root
COPY --from=builder /wheels /wheels
COPY requirements-dev.lock .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements-dev.lock \
    && rm -rf /wheels
COPY tests/ ./tests/
COPY pytest.ini ./
COPY scripts/ ./scripts/
# Deployment files the tests pin (Keycloak serves HTTPS only; the realm fits
# Keycloak's columns; docs/operations.md and the launcher/edge source are
# checked by tests/test_swim_launcher.py -- see .dockerignore for what of
# jumpstart/ actually reaches this COPY). Test stage only; not in the shipped image.
COPY docker-compose.yml ./
COPY keycloak/realm-nas-sentinel.json ./keycloak/
COPY .streamlit/secrets.toml.example .streamlit/config.toml ./.streamlit/
COPY docs/ ./docs/
COPY monitoring/ ./monitoring/
COPY jumpstart/ ./jumpstart/
RUN chown -R sentinel:sentinel /app/tests /app/scripts
USER sentinel
CMD ["python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]

# The last stage is the default build target, so the shipped image must be
# last: `docker compose build` and a plain `docker build` produce it.
FROM runtime AS final
