#!/usr/bin/env bash
# CI-faithful local run: build the test image from a clean LF copy of a tree, then run ruff, mypy and the suite
# INSIDE the unmounted image, as .github/workflows/pyapp-ci.yml does. Mounting a tree over /app instead hides any
# file the image lacks (that is how a test reading .env.example passed locally and failed in CI).
#
# Usage:
#   CI_WORK_DIR=<scratch dir outside the repo> WT_NAME=<tree dir under it> run-ci-like.sh [build|lint|test|infra|all|down]
#
#   CI_WORK_DIR  a scratch directory, as a Git Bash path (/c/...). Nothing here writes inside the repo.
#   WT_NAME      the name of the directory under CI_WORK_DIR that holds the tree to test, normally a worktree:
#                  git -c core.autocrlf=false -c core.eol=lf worktree add -b <branch> "$CI_WORK_DIR/<WT_NAME>" main
#
# Each WT_NAME gets its own image tag, network and Postgres container, so two runs with different names do not
# touch each other. Never run two runs with the SAME name at once. `down` removes this name's network and database.
# Run it in Git Bash (it needs cygpath), not WSL bash.
set -uo pipefail
export MSYS_NO_PATHCONV=1
: "${CI_WORK_DIR:?set CI_WORK_DIR to a scratch directory outside the repo}"
: "${WT_NAME:?set WT_NAME to the tree directory under CI_WORK_DIR}"
WT="$CI_WORK_DIR/$WT_NAME"; COPY="$CI_WORK_DIR/$WT_NAME-ci"
NET="$WT_NAME-cinet"; PG="$WT_NAME-cipg"; IMAGE="nas-feedops-sentinel:ci-$WT_NAME"
MODE="${1:-all}"

if [ "$MODE" = "down" ]; then
  docker rm -f "$PG" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; echo "removed $WT_NAME ci containers"; exit 0
fi
[ -d "$WT" ] || { echo "no tree at $WT"; exit 1; }

if [ "$MODE" = "all" ] || [ "$MODE" = "build" ]; then
  rm -rf "$COPY"; mkdir -p "$COPY"
  # Tracked and untracked-but-not-ignored files only, minus anything that could be a secret.
  ( cd "$WT" && git ls-files -z -co --exclude-standard \
    | grep -zvE '(^|/)(\.env|secrets\.toml)$|(^|/)\.venv|^nas-feedops-sentinel/tls/|\.(key|pem|crt)$|^ceremony/' \
    | tar --null -T - -cf - 2>/dev/null ) | tar -xf - -C "$COPY"
  # core.autocrlf can leave CRLF behind; strip it from text files so shell scripts and pins compare the same as in CI.
  find "$COPY" -type f \( -name '*.py' -o -name '*.yml' -o -name '*.yaml' -o -name '*.md' -o -name '*.json' -o -name '*.toml' \
    -o -name '*.sh' -o -name '*.txt' -o -name '*.conf' -o -name '*.ini' -o -name '*.hcl' -o -name '*.tf' -o -name '*.ps1' \
    -o -name '*.html' -o -name '*.js' -o -name '*.css' -o -name '*.example' -o -name '*.cnf' -o -name '*.java' -o -name '*.tmpl' \
    -o -name '*.lock' -o -name 'Dockerfile' \) -exec sed -i 's/\r$//' {} +
  echo "=== build test image ($IMAGE)"
  docker build -q --target test -f "$(cygpath -w "$COPY/nas-feedops-sentinel/docker/Dockerfile")" -t "$IMAGE" "$(cygpath -w "$COPY/nas-feedops-sentinel")" || exit 1
  [ "$MODE" = "build" ] && exit 0
fi

APP_WIN="$(cygpath -w "$COPY/nas-feedops-sentinel")"
REPO_WIN="$(cygpath -w "$COPY")"

if [ "$MODE" = "all" ] || [ "$MODE" = "lint" ]; then
  echo "=== ruff"; docker run --rm -v "$APP_WIN:/w:ro" -w /w "$IMAGE" ruff check --no-cache src tests scripts dashboard || exit 1
  echo "=== mypy"; docker run --rm -v "$APP_WIN:/w:ro" -w /w "$IMAGE" python -m mypy --cache-dir=/tmp/mypy-cache || exit 1
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "test" ]; then
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  docker inspect "$PG" >/dev/null 2>&1 || docker run -d --name "$PG" --network "$NET" -e POSTGRES_USER=sentinel_ci \
    -e POSTGRES_PASSWORD=ci-only-not-a-secret -e POSTGRES_DB=sentinel_ci postgres:16 >/dev/null
  # -h 127.0.0.1 on purpose: over the unix socket pg_isready succeeds against initdb's temporary server, which then
  # restarts, so a loop that breaks on it races. The temporary server does not listen on TCP.
  for i in $(seq 1 40); do docker exec "$PG" pg_isready -h 127.0.0.1 -U sentinel_ci -d sentinel_ci >/dev/null 2>&1 && break; sleep 1; done
  echo "=== suite (inside the unmounted image, with coverage, as CI)"
  docker run --rm --network "$NET" -e POSTGRES_HOST="$PG" -e POSTGRES_PORT=5432 -e POSTGRES_USER=sentinel_ci \
    -e POSTGRES_PASSWORD=ci-only-not-a-secret -e POSTGRES_DB=sentinel_ci -e DATABASE_URL= \
    -e VAULT_AGENT_ENV_FILE= -e CRITIC_ENABLED=0 -e COVERAGE_FILE=/tmp/.coverage "$IMAGE" sh -c '
      python -m pytest -p no:cacheprovider -q --cov=src --cov=dashboard --cov-branch --cov-report=term-missing:skip-covered &&
      echo "src/ coverage (floor 95%):" &&
      coverage report --include="src/*" --fail-under=95 --format=total'
  rc=$?; echo "suite exit $rc"; [ $rc -ne 0 ] && exit $rc
fi

if [ "$MODE" = "all" ] || [ "$MODE" = "infra" ]; then
  echo "=== infra tests + full-checkout step (as CI)"
  docker run --rm -v "$REPO_WIN:/repo:ro" -w /repo/instraclustr "$IMAGE" python -m pytest -p no:cacheprovider -q -o addopts= tests || exit 1
  docker run --rm -v "$REPO_WIN:/repo:ro" -w /repo/nas-feedops-sentinel -e PYTHONPATH=/repo/nas-feedops-sentinel -e REQUIRE_FULL_CHECKOUT=1 \
    "$IMAGE" python -m pytest -p no:cacheprovider --noconftest -q -o addopts= tests/test_framework_mapping.py || exit 1
fi
echo "=== done"
