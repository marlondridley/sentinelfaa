# AGENTS.md

Instructions for any agent working in this repository, including the software factory's agents. Humans: the
[README](README.md) is the overview; this file is the working rules and the commands.

## What this is

PCT TAIS FeedOps Sentinel: feed-trust monitoring and human-in-the-loop escalation for live FAA SWIM data across
Potomac TRACON. Models explain and argue; rules and people decide. No code path lets model output change an action's
status.

| Path | What is there |
|---|---|
| `nas-feedops-sentinel/` | The application. `src/` (ingest, pipeline, agents, storage, API), `dashboard/` (Streamlit), `tests/`, `policies/`, `prompts/`, `docs/`, `docker/`, `docker-compose.yml` |
| `instraclustr/` | Identity and infrastructure: Vault, SPIRE, certificate ceremony, its own compose file and tests |
| `.github/` | CI workflows and scripts |
| `.agents/skills/` | Project procedures (edit here). `.claude/skills/` is a generated copy: `python scripts/sync_agent_skills.py` |
| `launch_nas_sentinel.ps1` | Brings up the **live** local stack. Never run it |

Read before you touch an area:

- Dashboard (`nas-feedops-sentinel/dashboard/`, `src/review_queue.py`): the `dashboard-rules` skill. One job per page, every count clickable and equal to its rows, every table row says who acts and what to do, in plain words. The look is approved; do not restyle.
- An agent, its prompt, tools or model use: the `add-an-agent` skill and `nas-feedops-sentinel/docs/agent-governance.md`. The agent register (`policies/agent-register.yaml`) and the grants in `policies/model-use.yaml` and `policies/tool-use.yaml` must agree with the code.
- Data going in or on screen: the `real-data-only` skill.
- Tests and CI: the `ci-like-test-run` skill. `.github/workflows/pyapp-ci.yml` is authoritative; if they disagree, the workflow wins.
- Operations, certificates, secrets, the live feed: `nas-feedops-sentinel/docs/operations.md` and `docs/runbooks.md`.

## Never

- Touch the live stack: no `launch_nas_sentinel.ps1`, no `docker compose up/down/restart` on the repository's compose files, no writes to a running container, no starting Docker Desktop (starting it restarts the live stack and its public tunnel; ask the human).
- Load synthetic, demo or sample data anywhere. Only real FAA SWIM data goes in: replayed captures from `tests/fixtures/` or the live feed. There is no synthetic mode.
- Read, print, copy or commit secrets: `.env`, `.streamlit/secrets.toml`, keys, certificates, `tls/`, `ceremony/`, Vault output. The test credentials in `pyapp-ci.yml` are throwaway and fine to use.
- Push, force-push, open a pull request, or touch a remote. The factory merges locally and stops.
- Open or edit `capgemini-*.md`, `BUILD_PROMPT.md`, `RAG_System/`, `demo-briefing/` or `llm-wiki/`. They are the owner's, not part of the product.
- Call a model provider directly. Every model call goes through `src/llm_client.py` to the LiteLLM proxy (`ruff` rule TID251 enforces it).
- Use a naive datetime. Every datetime is UTC-aware (`ruff` rule DTZ). A naive one is read as local time by asyncpg and shifts by the host's offset.
- Lower the `src/` coverage floor (95%), skip or weaken a test to make it pass, or turn a strict `xfail` into a pass without fixing the bug it documents.
- Add or change a dependency without regenerating the hash-locked `requirements*.lock` as `requirements.txt` describes, and recording any CVE exception in `cve-allowlist.md`.

## Set up a fresh checkout

Nothing to install on the host. The code is Python 3.12 with hash-locked dependencies, and all of it lives inside the
Docker test image; the host has other Python versions, so do not create a virtualenv or `pip install` here.

What you need is **Docker Desktop running**. If `docker info` fails, stop and tell the human; do not start it.

The first command in a job is the build below. It builds the image from the worktree and takes a few minutes.

## Run lint, types, tests and build

`.agents/skills/ci-like-test-run/scripts/run-ci-like.sh` is the one way to do all of it. It builds the test image from a
clean LF copy of your tree (tracked and untracked-but-not-ignored files, minus anything secret-looking), then runs
ruff, mypy and pytest inside the image on a throwaway Postgres 16, as CI does. Run it in **Git Bash**, never WSL bash.
A factory worktree is already `<parent>/<name>-factory/<job-id>`, so:

```bash
WT=<the job's worktree, as a Git Bash path>
export CI_WORK_DIR="$(dirname "$WT")" WT_NAME="$(basename "$WT")"       # WT_NAME is the job id, so runs never collide
S="$WT/.agents/skills/ci-like-test-run/scripts/run-ci-like.sh"
bash "$S" build        # build the test image from the worktree; this is the "build" step
bash "$S" lint         # ruff check src tests scripts dashboard, then mypy (rules in ruff.toml and mypy.ini)
bash "$S" test         # the full pytest suite with coverage; src/ must stay at or above 95%
bash "$S" infra        # instraclustr/tests and the framework-mapping check (run it if you touched instraclustr/, docs or workflows)
bash "$S" down         # always finish with this: removes this job's database and network
```

`all` runs build, lint, test and infra and takes about ten minutes. Run long steps in the background and wait for them.
Read the pytest summary line (passed, skipped, xfailed), not just the exit code. Only commits and working-tree files in
the worktree are tested, so build before you commit and again after the last change.

Single test while working: after `build`, `docker run --rm --network "$WT_NAME-cinet" -e POSTGRES_HOST="$WT_NAME-cipg" -e POSTGRES_PORT=5432 -e POSTGRES_USER=sentinel_ci -e POSTGRES_PASSWORD=ci-only-not-a-secret -e POSTGRES_DB=sentinel_ci -e DATABASE_URL= -e VAULT_AGENT_ENV_FILE= -e CRITIC_ENABLED=0 "nas-feedops-sentinel:ci-$WT_NAME" python -m pytest -p no:cacheprovider -q -o addopts= tests/test_x.py` (start the database first with `test`, or `docker start "$WT_NAME-cipg"`). The suite truncates every table and refuses a database whose name lacks `test`, `ci`, `scratch` or `tmp`; never point it elsewhere.

Never run two runs with the same `WT_NAME`, and never two pytest processes against one database.

## Start the dev server on a given port

Only the dashboard has pages. Use the job's port, passed in the prompt, bound to loopback so other jobs and the network
cannot reach it. It runs from the image `run-ci-like.sh build` made, on the job's own database, with the sign-in
requirement off (the lab setting `docker-compose.yml` documents for a run with no Keycloak):

```bash
docker run -d --rm --name "$WT_NAME-dash" --network "$WT_NAME-cinet" -p "127.0.0.1:<port>:8501" \
  -e POSTGRES_HOST="$WT_NAME-cipg" -e POSTGRES_PORT=5432 -e POSTGRES_USER=sentinel_ci \
  -e POSTGRES_PASSWORD=ci-only-not-a-secret -e POSTGRES_DB=sentinel_ci -e DATABASE_URL= \
  -e VAULT_AGENT_ENV_FILE= -e CRITIC_ENABLED=0 -e REQUIRE_SIGN_IN=0 \
  "nas-feedops-sentinel:ci-$WT_NAME" \
  python -m dashboard.serve --server.address=0.0.0.0 --server.port=8501 --server.headless=true
# page: http://127.0.0.1:<port>   stop it with: docker rm -f "$WT_NAME-dash"
```

**Not yet exercised.** This recipe is written from `docker-compose.yml` and the Dockerfile; Docker was off when it was
written. The database starts empty, so the pages show their empty states, and they may need the schema first
(`python -m src.storage.migrate` inside the container). Whoever first needs a page confirms the recipe and corrects this
section. If you cannot get a page up, say so in your file; do not describe a page you did not see. Do not load demo data
to fill the pages (see Never). Do not sign in or submit forms.

## Conventions

- Match the surrounding code: idioms, naming, formatting and comment density. The code states its reasons in comments, in plain prose; do the same where a reason is not obvious.
- Make the simplest change that meets the spec. No fallbacks, hidden defaults or extras the spec does not ask for. Fail closed.
- Tests run for real: `asyncio_mode = auto`, strict `xfail` for documented bugs (`tests/test_agt_known_bugs.py`). Add tests with the change; the change is not done until the suite passes.
- A change to behaviour updates the docs that describe it (`README.md`, `nas-feedops-sentinel/docs/`, `API_DEPLOYMENT.md`, `policies/`) and the framework mapping if it cites a file you moved (`tests/test_framework_mapping.py` checks the paths).
- Text a person reads is plain words: define a term the first time, say who acts and what to do, one decision per message.
- Line endings are LF in the repository. The Windows checkout may show CRLF; do not commit a whole-file ending change.
- Commits: short one-line messages in the style of `git log --oneline`, explicit paths (never `git add -A`), one commit per distinct piece of work.

## Always escalate to a human

The approver sends these to a human even when every review passes. On top of the generic list (money, access, identity,
CI or deploy config, schema migrations, dependencies, product decisions), for Sentinel:

- Anything that lets a model, or a new code path, stage, approve, reject or execute an action; any change to `src/execution_gate.py`, `src/auth.py`, `src/keycloak_auth.py` or the roles (`watch_officer`, `supervisor`, `auditor`).
- `policies/*.yaml` (model use, tool use, agent register, SLOs), prompts in `prompts/`, `src/llm_client.py`, `src/governance.py`, `src/redaction.py`, `src/guardrails.py`, or a new agent or tool grant.
- Storage schema or `src/storage/migrate.py`; anything that writes to the evidence tables other than through the existing repositories.
- `docker-compose.yml`, `docker/`, `instraclustr/`, `.github/`, `monitoring/` (Prometheus rules, Alertmanager routes, Slack), `keycloak/`, TLS, SPIFFE or SPIRE, Vault, `launch_nas_sentinel.ps1`.
- `requirements*.lock`, `cve-allowlist.md`, or any change that lowers a gate, a coverage floor or a lint rule.
- Anything that moves data across a boundary the README lists under "Boundaries", or changes what a screen claims about the feed.
