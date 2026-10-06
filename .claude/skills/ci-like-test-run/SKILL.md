---
name: ci-like-test-run
description: Run Sentinel's ruff, mypy, pytest suite and infra tests locally the way GitHub CI does, in Docker on a throwaway Postgres, without touching the live stack or any secrets. Use before merging, when CI is unavailable (the repo is private and Actions is billing-blocked), when a test reads a repo file, when a test passes locally but may fail in CI, or when asked to verify, run the suite, check coverage, or hunt a flaky test.
compatibility: Windows 11 with Docker Desktop running, and Git Bash (not WSL bash). arm64 host. The full run takes about 10 minutes.
metadata:
  area: testing
---

# Running the suite like CI

The suite needs Postgres and truncates tables, so it always runs in Docker against a **throwaway** `postgres:16` on its own network. Never point it at the live stack's database.

The authoritative commands are in `.github/workflows/pyapp-ci.yml`. If this skill and the workflow disagree, the workflow wins; fix the skill.

## Use the faithful path

Mounting your working tree over `/app` is quick but hides any file the test image does not contain. A test that reads `.env.example` passed that way and then failed four cases in CI, because the Dockerfile's test stage never copies that file. So:

1. Make a clean copy of the tree, **tracked files only, LF line endings, no secrets**.
2. Build the test image **from that copy**.
3. Run ruff, mypy and pytest **inside the unmounted image**, as the workflow does.

`scripts/run-ci-like.sh`, in this skill's own directory, does all three. Paths below are relative to the directory that holds this `SKILL.md`, so they work wherever your tool installs the skill (`.agents/skills/ci-like-test-run/` in this repo):

```bash
SKILL_DIR=<the directory holding this SKILL.md>
export CI_WORK_DIR=/c/path/to/a/scratch/dir      # outside the repo
git -c core.autocrlf=false -c core.eol=lf worktree add -b <branch> "$CI_WORK_DIR/<name>" <branch-or-main>
WT_NAME=<name> bash "$SKILL_DIR/scripts/run-ci-like.sh" all     # or: build | lint | test | infra
WT_NAME=<name> bash "$SKILL_DIR/scripts/run-ci-like.sh" down    # remove its database and network
```

That worktree form checks files out as stored (LF), contains tracked files only so no `.env` or keys can come along, and leaves the user's dirty checkout untouched. Edits, `git add` and `git commit` inside it are fine (git only warns about CRLF).

Run `all` in the background. Do not start a second run with the same `WT_NAME`: they share one database container and would truncate each other's tables. Different names are isolated (own image tag, network and database).

The script mirrors CI's steps: `ruff check src tests scripts dashboard`, `mypy`, the full pytest run with coverage, then `coverage report --include="src/*" --fail-under=95`, then the `instraclustr/tests` infra tests with the whole copy mounted at `/repo`, then `tests/test_framework_mapping.py` with `REQUIRE_FULL_CHECKOUT=1`.

## Reading the result

- **Read the pytest summary line, not just the exit code.** Compare passed/skipped/xfailed to the last known run.
- **Only `src/` coverage gates (floor 95%).** The coverage table lists `src` and `dashboard` together at a lower figure; that is reported, not gated.
- **A different number of collected tests after a big edit means a parametrized case was lost.** Find it with `pytest --collect-only -q` in the old and new images and `comm` the two lists.
- `tests/test_ingest_output_java.py` needs a JDK and the git-ignored kit jar, so it skips in the Docker image. It runs only locally with `--noconftest -o addopts=`.
- If a run says **CAUGHT for every mutant**, suspect the harness, not the tests (see WSL bash below).

## Traps (each one has already cost time)

- **Docker Desktop may be off, and starting it can restart the live stack** (`restart: unless-stopped` brings back the public tunnel and the SPIRE agent). Ask the user to start it. Do not start it yourself.
- **The `:test` image goes stale.** An old one predated `defusedxml` and 11 modules failed to collect with `ModuleNotFoundError`. The script builds its own tagged image from the copy, so this only bites when you reuse `nas-feedops-sentinel:test`. If you rebuild that tag by hand, use `docker build --target test -t nas-feedops-sentinel:test -f docker/Dockerfile .` from the sanitized copy. It retags only `:test`, never the live `:latest`.
- **Secrets in the copy.** `.env`, `.streamlit/secrets.toml`, keys, certs and `.venv` must stay out. With secrets present, Streamlit page tests see a configured sign-in and fail, and real credentials sit next to test code.
- **CRLF.** The copy comes out CRLF when `core.autocrlf` is on. `sed -i 's/\r$//'` through `xargs` did not strip it; `find <dir> -type f -exec dos2unix -q {} +` does. Count CRs with `tr -cd '\015' | wc -c`. A `$'\r'` inside `$(...)` can be mangled by an agent's shell tool, and `grep` then reports nonsense.
- **WSL bash.** `subprocess.run(["bash", ...])` from Windows Python resolves to WSL's bash and silently does nothing, so a mutation-check loop reports every mutant caught. Call `C:\Program Files\Git\bin\bash.exe`. To mutation-check, edit a file, run, and restore a saved copy.
- **Do not mount the tree `:ro` over `/app`:** tests write under `/app/data`. (The CI-like script mounts only for the read-only lint and infra steps, as CI does.)
- **Two pytest processes on one database.** Each truncates. Never.
- **Paths for `docker -v`.** In Git Bash set `MSYS_NO_PATHCONV=1` and pass Windows paths from `cygpath -w`. Export `MSYS_NO_PATHCONV=1` only on the `docker` line if a script also calls Windows `python.exe`, which needs `/c/...` left alone.
- **Heredocs through an agent's shell tool can mangle backslashes** (`\b` became a backspace). Create script files with your file-write or edit tool instead.
- **Recursive `grep -r` from the repo root hangs** (huge `.venv`, `reference/`). Use `git grep` or your tool's own search.
- **Throwaway Postgres readiness.** `pg_isready` over the unix socket succeeds against initdb's temporary server, which then restarts. Use `pg_isready -h 127.0.0.1`; the temporary server does not listen on TCP.

## Hunting a flaky test

Wrap the suspect functions with timestamps in a throwaway script, run it inside the image with no repo edit, and use a repeat runner that starts a **fresh container per run**. Tests that run under `--noconftest` have no database in the `:test` image, so anything touching the DB logs a connection-refused error within milliseconds. That is a red herring in failure reports, not a delay.

## Report

Say what tree and commit you tested, which steps ran (ruff, mypy, suite, coverage, infra), the pytest summary line, the `src/` coverage figure, and anything skipped. If you could not run it (Docker off, no time), say so; do not report a pass you did not see.

When CI is not running (the repo is private and Actions is billing-blocked), this run is the only evidence a change works. Say that it is a local run, not a CI result.
